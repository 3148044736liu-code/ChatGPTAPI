"""Runtime compatibility for Windows hosts enforcing signed native extensions."""
from pydantic import BaseModel

if not hasattr(BaseModel, "model_dump"):
    BaseModel.model_dump = BaseModel.dict