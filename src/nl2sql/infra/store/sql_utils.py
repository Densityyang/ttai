"""SQL 文本扫描工具。"""

from __future__ import annotations

import sqlparse
from sqlparse import tokens as T


def find_sql_comment_marker(fragment: str) -> str | None:
    """查找 SQL 片段中首个未被引号包裹的注释标记。"""
    parsed = sqlparse.parse(fragment)
    if not parsed:
        return None

    for statement in parsed:
        for token in statement.flatten():
            if token.ttype in (T.Comment.Single, T.Comment.Multiline):
                value = token.value.strip()
                if value.startswith("--"):
                    return "--"
                if value.startswith("/*"):
                    return "/*"
                if value.startswith("*/"):
                    return "*/"
    return None


def mask_sql_literals_and_comments(sql: str) -> str:
    """将 SQL 中字符串字面量与注释替换为空格，保留换行。"""
    parsed = sqlparse.parse(sql)
    if not parsed:
        return sql

    result: list[str] = []
    for statement in parsed:
        for token in statement.flatten():
            if token.ttype in (T.String.Single, T.String.Symbol, T.Literal.String.Single):
                result.append(_mask_preserving_newlines(token.value))
            elif token.ttype in (T.Comment.Single, T.Comment.Multiline):
                result.append(_mask_preserving_newlines(token.value))
            else:
                result.append(token.value)

    return "".join(result)


def _mask_preserving_newlines(text: str) -> str:
    """将文本替换为空格，但保留换行符。"""
    return "".join("\n" if char == "\n" else " " for char in text)
