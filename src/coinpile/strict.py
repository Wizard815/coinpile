"""The base for every request body: numbers must be real numbers of a size money can have. JSON lets 1e999 through as infinity, and one
infinite (or absurdly large) amount stored in a row makes every page that adds it up fail."""
import pydantic

MAX_NUMBER = 1e12


class BaseModel(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(allow_inf_nan=False)

    @pydantic.field_validator("*")
    @classmethod
    def _in_range(cls, v):
        if isinstance(v, (int, float)) and not isinstance(v, bool) and abs(v) > MAX_NUMBER:
            raise ValueError("that number is too large")
        return v
