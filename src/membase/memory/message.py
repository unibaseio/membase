# -*- coding: utf-8 -*-
# mypy: disable-error-code="misc"
"""The base class for message unit"""
from typing import (
    Any,
    Literal,
    Union,
    List,
    Optional,
    Tuple
)

import datetime
import hashlib
from uuid import uuid4

from loguru import logger

from .serialize import is_serializable

def _get_timestamp(
    format_: str = "%Y-%m-%d %H:%M:%S",
    time: datetime.datetime = None,
) -> str:
    """Get current timestamp."""
    if time is None:
        return datetime.datetime.now().strftime(format_)
    else:
        return time.strftime(format_)

def _map_string_to_color_mark(
    target_str: str,
) -> Tuple[str, str]:
    """Map a string into an index within a given length.

    Args:
        target_str (`str`):
            The string to be mapped.

    Returns:
        `Tuple[str, str]`: A color marker tuple
    """
    color_marks = [
        ("\033[90m", "\033[0m"),
        ("\033[91m", "\033[0m"),
        ("\033[92m", "\033[0m"),
        ("\033[93m", "\033[0m"),
        ("\033[94m", "\033[0m"),
        ("\033[95m", "\033[0m"),
        ("\033[96m", "\033[0m"),
        ("\033[97m", "\033[0m"),
    ]

    hash_value = int(hashlib.sha256(target_str.encode()).hexdigest(), 16)
    index = hash_value % len(color_marks)
    return color_marks[index]

class Message:
    """The message class, which is responsible for storing
    the information of a message, including

    - id:           the identity of the message
    - name:         who sends the message
    - content:      the message content
    - role:         the sender role chosen from 'system', 'user', 'assistant'
    - url:          the url(s) refers to multimodal content
    - metadata:     some additional information
    - timestamp:    when the message is created
    """

    __serialized_attrs: set = {
        "id",
        "name",
        "content",
        "role",
        "url",
        "metadata",
        "timestamp",
        "type",  # 新增type字段
    }
    """The attributes that need to be serialized and deserialized."""

    def __init__(
        self,
        name: str,
        content: Any,
        role: Union[str, Literal["system", "user", "assistant"]],
        url: Optional[Union[str, List[str]]] = None,
        metadata: Optional[Union[dict, str]] = None,
        echo: bool = False,
        type: str = "stm",  # 新增type参数，默认stm
        **kwargs: Any,
    ) -> None:
        """Initialize the message object.

        There are two ways to initialize a message object:
        - Providing `name`, `content`, `role`, `url`(Optional),
          `metadata`(Optional) to initialize a normal message object.
        - Providing `host`, `port`, `task_id` to initialize a placeholder.

        Normally, users only need to create a normal message object by
        providing `name`, `content`, `role`, `url`(Optional) and `metadata`
        (Optional).

        The initialization of message has a high priority, which means that
        when `name`, `content`, `role`, `host`, `port`, `task_id` are all
        provided, the message will be initialized as a normal message object
        rather than a placeholder.

        Args:
            name (`str`):
                The name of who generates the message.
            content (`Any`):
                The content of the message.
            role (`Union[str, Literal["system", "user", "assistant"]]`):
                The role of the message sender.
            url (`Optional[Union[str, List[str]]`, defaults to `None`):
                The url of the message.
            metadata (`Optional[Union[dict, str]]`, defaults to `None`):
                The additional information stored in the message.
            echo (`bool`, defaults to `False`):
                Whether to print the message when initializing the message obj.
        """

        self.id = uuid4().hex
        self.name = name
        self.content = content
        self.role = role
        self.url = url
        self.metadata = metadata
        self.timestamp = _get_timestamp()
        self.type = type

        if kwargs:
            logger.warning(
                f"In current version, the message class does not"
                f" inherit the dict class. "
                f"The input arguments {kwargs} are not used.",
            )

        if echo:
            logger.chat(self)

    def __getitem__(self, item: str) -> Any:
        """The getitem function, which will be deprecated in the new version"""
        logger.warning(
            f"The Message class doesn't inherit dict any more. Please refer to "
            f"its attribute by `message.{item}` directly."
            f"The support of __getitem__ will also be deprecated in the "
            f"future.",
        )
        return self.__getattribute__(item)

    @property
    def id(self) -> str:
        """The identity of the message."""
        return self._id

    @property
    def name(self) -> str:
        """The name of the message sender."""
        return self._name

    @property
    def _colored_name(self) -> str:
        """The name around with color marks, used to print in the terminal."""
        m1, m2 = _map_string_to_color_mark(self.name)
        return f"{m1}{self.name}{m2}"

    @property
    def content(self) -> Any:
        """The content of the message."""
        return self._content

    @property
    def role(self) -> Literal["system", "user", "assistant"]:
        """The role of the message sender, chosen from 'system', 'user',
        'assistant'."""
        return self._role

    @property
    def url(self) -> Optional[Union[str, List[str]]]:
        """A URL string or a list of URL strings."""
        return self._url

    @property
    def metadata(self) -> Optional[Union[dict, str]]:
        """The metadata of the message, which can store some additional
        information."""
        return self._metadata

    @property
    def timestamp(self) -> str:
        """The timestamp when the message is created."""
        return self._timestamp

    @property
    def type(self) -> str:
        """The type of the message: 'stm', 'ltm', or 'profile'."""
        return self._type

    @id.setter  # type: ignore[no-redef]
    def id(self, value: str) -> None:
        """Set the identity of the message."""
        self._id = value

    @name.setter  # type: ignore[no-redef]
    def name(self, value: str) -> None:
        """Set the name of the message sender."""
        self._name = value

    @content.setter
    def content(self, value: Any) -> None:
        """Set the content of the message."""
        if not is_serializable(value):
            try:
                value = str(value)
            except Exception as e:
                raise ValueError(
                    f"Content of type {type(value)} is not serializable and "
                    f"cannot be converted to string: {str(e)}"
                )
        self._content = value

    @role.setter  # type: ignore[no-redef]
    def role(self, value: Literal["system", "user", "assistant"]) -> None:
        """Set the role of the message sender. The role must be one of
        'system', 'user', 'assistant'."""
        if value not in ["system", "user", "assistant"]:
            raise ValueError(
                f"Invalid role {value}. The role must be one of "
                f"['system', 'user', 'assistant']",
            )
        self._role = value

    @url.setter  # type: ignore[no-redef]
    def url(self, value: Union[str, List[str], None]) -> None:
        """Set the url of the message. The url can be a URL string or a list of
        URL strings."""
        self._url = value

    @metadata.setter  # type: ignore[no-redef]
    def metadata(self, value: Union[dict, str, None]) -> None:
        """Set the metadata of the message to store some additional
        information."""
        self._metadata = value

    @timestamp.setter  # type: ignore[no-redef]
    def timestamp(self, value: str) -> None:
        """Set the timestamp of the message."""
        self._timestamp = value

    @type.setter
    def type(self, value: str) -> None:
        if value not in ["stm", "ltm", "profile"]:
            raise ValueError(f"Invalid type {value}. Must be 'stm', 'ltm' or 'profile'.")
        self._type = value

    def formatted_str(self, colored: bool = False) -> str:
        """Return the formatted string of the message. If the message has an
        url, the url will be appended to the content.

        Args:
            colored (`bool`, defaults to `False`):
                Whether to color the name of the message

        Returns:
            `str`: The formatted string of the message.
        """
        if colored:
            name = self._colored_name
        else:
            name = self.name

        colored_strs = [f"{name}: {self.content}"]
        if self.url is not None:
            if isinstance(self.url, list):
                for url in self.url:
                    colored_strs.append(f"{name}: {url}")
            else:
                colored_strs.append(f"{name}: {self.url}")
        return "\n".join(colored_strs)

    def __eq__(self, value: object) -> bool:
        return (
            isinstance(value, Message)
            and self.id == value.id
            and self.name == value.name
            and self.content == value.content
            and self.role == value.role
            and self.url == value.url
            and self.metadata == value.metadata
            and self.timestamp == value.timestamp
        )

    def to_dict(self) -> dict:
        """Serialize the message into a dictionary, which can be
        deserialized by calling the `from_dict` function.

        Returns:
            `dict`: The serialized dictionary.
        """
        seen_objects = set()

        def _serialize_with_cycle_detection(obj):
            obj_id = id(obj)
            if obj_id in seen_objects:
                return str(obj)
            seen_objects.add(obj_id)
            
            if isinstance(obj, (str, int, float, bool, type(None))):
                return obj
            elif isinstance(obj, (list, tuple)):
                return [_serialize_with_cycle_detection(item) for item in obj]
            elif isinstance(obj, dict):
                return {str(k): _serialize_with_cycle_detection(v) for k, v in obj.items()}
            else:
                return str(obj)

        serialized_dict = {
            "__module__": self.__class__.__module__,
            "__name__": self.__class__.__name__,
        }

        for attr_name in self.__serialized_attrs:
            value = getattr(self, f"_{attr_name}")
            serialized_dict[attr_name] = _serialize_with_cycle_detection(value)

        return serialized_dict

    @classmethod
    def from_dict(cls, serialized_dict: dict) -> "Message":
        """Deserialize the dictionary to a Message object.

        Args:
            serialized_dict (`dict`):
                A dictionary that must contain the keys in
                `Message.__serialized_attrs`, and the keys `__module__` and
                `__name__`.

        Returns:
            `Message`: A Message object.
        """
        assert set(
            serialized_dict.keys(),
        ) == cls.__serialized_attrs.union(
            {
                "__module__",
                "__name__",
            },
        ), (
            f"Expect keys {cls.__serialized_attrs}, but get "
            f"{set(serialized_dict.keys())}",
        )

        assert serialized_dict.pop("__module__") == cls.__module__
        assert serialized_dict.pop("__name__") == cls.__name__

        obj = cls(
            name=serialized_dict["name"],
            content=serialized_dict["content"],
            role=serialized_dict["role"],
            url=serialized_dict["url"],
            metadata=serialized_dict["metadata"],
            echo=False,
            type=serialized_dict.get("type", "stm"),  # 兼容老数据
        )
        obj.id = serialized_dict["id"]
        obj.timestamp = serialized_dict["timestamp"]
        return obj
