from fastapi.templating import Jinja2Templates

from app.config import BASE_DIR
from app.static_version import STATIC_VERSION

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals["static_v"] = STATIC_VERSION
