"""Shared limits for the browser and read-only search endpoint."""
DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 1000
PAGE_SIZES = (25,50,75,100)


def validate_page_size(value):
    if isinstance(value,bool) or not str(value).strip().isascii() or not str(value).strip().isdigit():
        raise ValueError('페이지당 표시 건수는 1~1,000 사이의 정수로 입력하세요.')
    size=int(value)
    if not 1<=size<=MAX_PAGE_SIZE:
        raise ValueError('페이지당 표시 건수는 1~1,000 사이의 정수로 입력하세요.')
    return size
