# -*- coding: utf-8 -*-
"""The serialization module for the package."""
import importlib
import json
from typing import Any


def _default_serialize(obj: Any) -> Any:
    """Serialize the object when `json.dumps` cannot handle it."""
    if hasattr(obj, "__module__") and hasattr(obj, "__class__"):
        # To avoid circular import, we hard code the module name here
        if (
            obj.__module__ == "membase.memory.message"
            and obj.__class__.__name__ == "Message"
        ):
            return obj.to_dict()

    return obj


def _deserialize_hook(data: dict) -> Any:
    """Deserialize the JSON string to an object, including Message object."""
    module_name = data.get("__module__", None)
    class_name = data.get("__name__", None)

    if module_name is not None and class_name is not None:
        module = importlib.import_module(module_name)
        cls = getattr(module, class_name)
        if hasattr(cls, "from_dict"):
            return cls.from_dict(data)
    return data


def serialize(obj: Any) -> str:
    """Serialize the object to a JSON string.

    This function supports to serialize `Message` object for now.
    """
    return json.dumps(obj, ensure_ascii=False, default=_default_serialize)


def deserialize(s: str) -> Any:
    """Deserialize the JSON string to an object

    This function supports to serialize `Message` object for now.
    """
    return json.loads(s, object_hook=_deserialize_hook)


def is_serializable(obj: Any) -> bool:
    """Check if the object is serializable."""
    try:
        serialize(obj)
        return True
    except Exception:
        return False
