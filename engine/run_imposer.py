import json
import sys
from types import SimpleNamespace
from imposer import process_imposer_pdf

settings = json.loads(sys.argv[3])
settings.update(input=sys.argv[1], output=sys.argv[2])
process_imposer_pdf(SimpleNamespace(**settings))
