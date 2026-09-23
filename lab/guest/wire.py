"""Lossless framing for bulky UART reports, leaving heartbeat frames small."""
import base64
import json
import zlib

LIMIT = 8 * 1024 * 1024


def encode(message):
    raw = json.dumps(message, separators=(',', ':')).encode()
    if len(raw) > LIMIT:
        raise ValueError('Serial report exceeds limit')
    if len(raw) > 16384:
        raw = json.dumps({'wire_encoding':'zlib-base64',
            'payload':base64.b64encode(zlib.compress(raw)).decode()},separators=(',', ':')).encode()
    return raw+b'\n'


def decode(message):
    if message.get('wire_encoding') != 'zlib-base64':
        return message
    compressed=base64.b64decode(message['payload'],validate=True)
    inflater=zlib.decompressobj()
    raw=inflater.decompress(compressed,LIMIT+1)
    if len(raw)>LIMIT or not inflater.eof or inflater.unused_data:
        raise ValueError('Invalid or oversized compressed serial report')
    return json.loads(raw)
