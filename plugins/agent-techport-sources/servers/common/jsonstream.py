"""Read the items of one large JSON array without loading the whole file.

TechPort's full project list is a 115 MB object {"results": [...], ...}.
json.load on it peaked at 900 MB of memory. This reads the file in chunks and
decodes one array item at a time. Standard library only.
"""

import json

_WS = " \t\r\n"


def iter_array(path, key, chunk_size=1 << 20):
    """Yield each item of the array stored under `key` in the JSON object at `path`.

    Finds the first occurrence of "key": [ in the file, so it is meant for files
    where that key belongs to the top-level object (TechPort puts "results" first).
    """
    decoder = json.JSONDecoder()
    marker = f'"{key}"'
    with open(path, encoding="utf-8") as f:
        buf = ""
        # Find "key" : [
        while True:
            block = f.read(chunk_size)
            if not block:
                raise ValueError(f"no {marker} array in {path}")
            buf += block
            at = buf.find(marker)
            if at == -1:
                buf = buf[-len(marker):]
                continue
            rest = buf[at + len(marker):].lstrip(_WS)
            if len(rest) < 2:
                continue            # need more to see ": ["
            if not rest.startswith(":"):
                raise ValueError(f"{marker} is not followed by ':' in {path}")
            rest = rest[1:].lstrip(_WS)
            if not rest:
                continue
            if not rest.startswith("["):
                raise ValueError(f"{marker} is not an array in {path}")
            buf = rest[1:]
            break

        pos = 0
        while True:
            # Skip separators between items.
            while True:
                while pos < len(buf) and buf[pos] in _WS + ",":
                    pos += 1
                if pos < len(buf):
                    break
                block = f.read(chunk_size)
                if not block:
                    raise ValueError(f"{path} ended inside the {marker} array")
                buf, pos = buf[pos:] + block, 0
            if buf[pos] == "]":
                return
            # Decode one item, reading more when it runs past the buffer.
            while True:
                try:
                    item, end = decoder.raw_decode(buf, pos)
                    break
                except json.JSONDecodeError:
                    block = f.read(chunk_size)
                    if not block:
                        raise
                    buf, pos = buf[pos:] + block, 0
            yield item
            pos = end
            if pos > chunk_size:            # drop what has been consumed
                buf, pos = buf[pos:], 0
