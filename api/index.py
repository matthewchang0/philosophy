from __future__ import annotations

import webapp


webapp.RUNS_ROOT.mkdir(parents=True, exist_ok=True)
webapp.init_auth_db()


class handler(webapp.AppHandler):
    pass
