from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

router = APIRouter()
templates = Jinja2Templates(directory="templates")


@router.get("/")
def home_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"request": request, "page_title": "Market Data Foundation"},
    )
