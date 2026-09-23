"""Loopback-only control panel routes."""

from dataclasses import dataclass, field
from pathlib import Path
import os
from typing import Any, Callable

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from gateway.app.models import ExitCreate, validate_name
from gateway.app.profiles import qr_png
from gateway.app.store import ClientRegistry
from gateway.app.profiles import AwgParameters, build_client_profile
from gateway.app.peers import AwgPeerManager


@dataclass
class StateStore:
    """In-process desired state; persistent backing is added by the installer."""

    exits: list[dict[str, Any]] = field(default_factory=list)
    clients: list[dict[str, Any]] = field(default_factory=list)
    profile_factory: Callable[[str], str] | None = None
    registry: ClientRegistry | None = None
    peer_manager: AwgPeerManager | None = None
    server_public_key: str = ""
    endpoint: str = ""
    exit_provisioner: Callable[[ExitCreate], dict[str, Any]] | None = None
    exit_remover: Callable[[dict[str, Any]], None] | None = None


def create_app(store: StateStore) -> FastAPI:
    """Create the UI; deployment binds Uvicorn to 127.0.0.1 only."""
    if store.registry:
        known_names = {client["name"] for client in store.clients}
        store.clients.extend({"name": name} for name in store.registry.names() if name not in known_names)
    app = FastAPI(docs_url=None, redoc_url=None)
    asset_root = Path(__file__).resolve().parents[1]
    templates = Jinja2Templates(directory=str(asset_root / "templates"))
    app.mount("/static", StaticFiles(directory=str(asset_root / "static")), name="static")

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request) -> HTMLResponse:
        healthy = sum(bool(exit_node.get("healthy")) for exit_node in store.exits)
        return templates.TemplateResponse(request, "dashboard.html", {"client_count": len(store.clients), "healthy": healthy, "exit_count": len(store.exits)})

    @app.get("/exits", response_class=HTMLResponse)
    def exits(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "exits.html", {"exits": store.exits})

    @app.get("/clients", response_class=HTMLResponse)
    def clients(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "clients.html", {"clients": store.clients})

    @app.post("/clients/new")
    def new_client(action: str = Form(...), name: str = Form("")) -> RedirectResponse:
        if action == "cancel":
            return RedirectResponse("/clients", status_code=303)
        if action != "create":
            raise HTTPException(422, "unsupported client form action")
        try:
            safe_name = validate_name(name)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if any(client["name"] == safe_name for client in store.clients):
            raise HTTPException(409, "client name already exists")
        profile = store.profile_factory(safe_name) if store.profile_factory else None
        public_key = ""
        if store.peer_manager and store.server_public_key and store.endpoint:
            keys = store.peer_manager.create_keys()
            public_key = keys.public_key
            address = f"10.20.0.{len(store.clients) + 2}/32"
            store.peer_manager.add(keys.public_key, address)
            profile = build_client_profile(safe_name, keys.private_key, address.replace("/32", "/24"), store.server_public_key, store.endpoint, AwgParameters(4, 8, 80, 25, 111, 234567, 345678, 456789, 567891))
        if store.registry and profile:
            store.registry.put(safe_name, profile, public_key)
        store.clients.append({"name": safe_name})
        return RedirectResponse("/clients", status_code=303)

    def profile_for(client_name: str) -> str:
        if not any(client["name"] == client_name for client in store.clients):
            raise HTTPException(404, "client not found")
        if store.profile_factory is None:
            if store.registry:
                profile = store.registry.get(client_name)
                if profile:
                    return profile
            raise HTTPException(503, "AmneziaWG key generator is not configured")
        if store.registry:
            profile = store.registry.get(client_name)
            if profile:
                return profile
        return store.profile_factory(client_name)

    @app.get("/clients/{client_name}.conf")
    def download_profile(client_name: str) -> PlainTextResponse:
        return PlainTextResponse(profile_for(client_name), headers={"Content-Disposition": f'attachment; filename="{client_name}.conf"'})

    @app.get("/clients/{client_name}/qr")
    def client_qr(client_name: str) -> Response:
        return Response(qr_png(profile_for(client_name)), media_type="image/png")

    @app.post("/clients/{client_name}/delete")
    def delete_client(client_name: str) -> RedirectResponse:
        selected = next((client for client in store.clients if client["name"] == client_name), None)
        if selected is None:
            raise HTTPException(404, "client not found")
        store.clients.remove(selected)
        if store.registry:
            if store.peer_manager and (public_key := store.registry.public_key(client_name)):
                store.peer_manager.remove(public_key)
            store.registry.delete(client_name)
        return RedirectResponse("/clients", status_code=303)

    @app.post("/exits/new")
    def new_exit(action: str = Form(...), name: str = Form(""), address: str = Form(""), password: str = Form("")) -> RedirectResponse:
        if action == "cancel":
            return RedirectResponse("/exits", status_code=303)
        if action != "create":
            raise HTTPException(422, "unsupported exit form action")
        try:
            request = ExitCreate(name=name, host=address, login="root", password=password)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if store.exit_provisioner is None:
            raise HTTPException(503, "exit provisioning is not configured")
        try:
            node = store.exit_provisioner(request)
        except Exception as exc:
            raise HTTPException(502, "foreign VPS provisioning failed") from exc
        store.exits.append(node)
        return RedirectResponse("/exits", status_code=303)

    @app.post("/exits/{exit_id}/delete", response_class=HTMLResponse)
    def delete_exit(exit_id: str, acknowledge: bool = Form(False)) -> Response:
        selected = next((node for node in store.exits if node["id"] == exit_id), None)
        if selected is None:
            raise HTTPException(404, "exit not found")
        healthy = [node for node in store.exits if node.get("healthy")]
        if selected.get("healthy") and len(healthy) == 1 and not acknowledge:
            return HTMLResponse("acknowledge loss of final healthy exit", status_code=409)
        if store.exit_remover is None:
            raise HTTPException(503, "exit removal is not configured")
        try:
            store.exit_remover(selected)
        except Exception as exc:
            raise HTTPException(502, "foreign VPS removal failed") from exc
        store.exits.remove(selected)
        return RedirectResponse("/exits", status_code=303)

    return app


# The installer currently supplies an in-memory store; a persistent backend is
# injected by the deployment entry point as it is introduced.
def production_store() -> StateStore:
    """Build the live store from root-owned gateway configuration."""
    state_dir = Path(os.getenv("AWG_GATEWAY_STATE_DIR", "/var/lib/awg-gateway"))
    server_key_path = Path(os.getenv("AWG_CLIENTS_PUBLIC_KEY", "/etc/amnezia/clients-public.key"))
    server_key = server_key_path.read_text(encoding="utf-8").strip() if server_key_path.exists() else ""
    return StateStore(
        exits=[{"id": "exit-01", "name": "Зарубежный VPS 01", "address": "153.76.194.217", "healthy": True}],
        registry=ClientRegistry(state_dir / "clients.sqlite3"),
        peer_manager=AwgPeerManager(),
        server_public_key=server_key,
        endpoint=os.getenv("AWG_CLIENT_ENDPOINT", "191.44.45.36:48221"),
    )


app = create_app(production_store())
