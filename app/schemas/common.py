from pydantic import BaseModel, Field


class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: str
    details: list[object] | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


class Page[T](BaseModel):
    items: list[T]
    total: int
    limit: int
    offset: int


class Message(BaseModel):
    status: str = Field(examples=["ok"])
