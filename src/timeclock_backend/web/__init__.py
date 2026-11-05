"""The shift corrections page (spec 2026-11-05): its own `timeclock-web` service, built from the backend's image.

Nothing here imports the backend's settings or database modules: the page must never hold the superuser password.
"""
