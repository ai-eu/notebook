from fastapi.templating import Jinja2Templates

from app.static_version import STATIC_VERSION

templates = Jinja2Templates(directory="templates")
templates.env.globals["static_v"] = STATIC_VERSION
