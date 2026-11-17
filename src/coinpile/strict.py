"""The base for every request body: numbers must be real numbers. JSON lets 1e999 through as infinity, and one infinite amount stored
in a row makes every page that adds it up fail."""
import pydantic


class BaseModel(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(allow_inf_nan=False)
