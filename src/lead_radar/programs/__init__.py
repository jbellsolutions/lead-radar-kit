"""Program lane: companies that already run an affiliate or partner program (partnership prospects).

Unlike the social lanes this finds companies by what their own site shows, not by what someone posted:
seed candidate domains (platform marketplaces, search dorks for platform portals), then verify each one by
fetching its site and matching platform fingerprints (config/program_fingerprints.yaml). Read-only, GET-only:
nothing here signs up, applies, or contacts anyone.
"""
