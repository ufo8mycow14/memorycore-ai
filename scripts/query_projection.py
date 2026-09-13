"""Separate explicit numeric tracking labels from self-contained search questions."""
import re

VERSION="numeric-tracking-label:self-contained-question:v1"
TRACKING=re.compile(r"(?i)^\s*(?:request\s+(?:reference|id)|correlation\s+id|trace\s+id|(?:question|query)\s+(?:number|id))\s*[:#]?\s*[0-9]+\s*:\s*((?:how|what|when|where|who|which|why|can|should|do|does|is|are)\b[^\r\n]+)$")
DEPENDENT=re.compile(r"(?i)\b(?:it|its|this|that|these|those|their)\b")


def search_query(query):
    match=TRACKING.fullmatch(query)
    if match and not DEPENDENT.search(match[1]):
        return match[1]
    return query
