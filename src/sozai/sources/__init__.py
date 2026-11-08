"""Asset libraries sozai reads. Each module exposes NAME and `fetch(http) -> Iterable[dict]`."""
from sozai.sources import ambientcg, megascans, polyhaven

ALL = {module.NAME: module for module in (polyhaven, ambientcg, megascans)}
