"""Start the ChiongWork web app without opening a browser: python sg-jobs/serve.py [port]"""
import sys

from sgjobs.web import run

run(int(sys.argv[1]) if len(sys.argv) > 1 else 8700, open_browser=False)
