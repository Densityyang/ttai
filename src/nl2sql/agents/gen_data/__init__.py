"""GenData Agent 包。"""

from .agent import get_gen_data_agent
from .service import query_database_with_gen_data_agent

__all__ = [
    "get_gen_data_agent",
    "query_database_with_gen_data_agent",
]
