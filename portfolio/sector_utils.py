"""
Sector and region normalization — canonical names used in user_settings targets.
"""

# Raw sector name → canonical target name
_MAP = {
    'Information Technology':  'Technology',
    'Technology':              'Technology',
    'Semiconductors':          'Technology',
    'Communication Services':  'Communication Services',
    'Telecom':                 'Communication Services',
    'Consumer Discretionary':  'Consumer Cyclical',
    'Consumer Cyclical':       'Consumer Cyclical',
    'Consumer':                'Consumer Cyclical',
    'Consumer Staples':        'Consumer Defensive',
    'Consumer Defensive':      'Consumer Defensive',
    'Staples':                 'Consumer Defensive',
    'Financials':              'Financial Services',
    'Financial Services':      'Financial Services',
    'Finance':                 'Financial Services',
    'Health Care':             'Healthcare',
    'Healthcare':              'Healthcare',
    'Industrials':             'Industrials',
    'Industrial':              'Industrials',
    'Materials':               'Basic Materials',
    'Basic Materials':         'Basic Materials',
    'Real Estate':             'Real Estate',
    'Energy':                  'Energy',
    'Utilities':               'Utilities',
}

# Canonical names — the authoritative list used in the UI and user_settings
ALL_SECTORS = [
    'Technology',
    'Financial Services',
    'Healthcare',
    'Consumer Cyclical',
    'Consumer Defensive',
    'Communication Services',
    'Energy',
    'Industrials',
    'Basic Materials',
    'Real Estate',
    'Utilities',
]


def normalize(raw: str) -> str:
    """Map any sector string to its canonical name; returns raw if unknown."""
    if not raw or str(raw).strip().lower() in ('', 'nan', 'none', 'unknown'):
        return 'Unknown'
    return _MAP.get(raw.strip(), raw.strip())


# ── Geographic regions ────────────────────────────────────────────────────────

# ISO-2 country code → canonical region name
REGION_MAP = {
    'US': 'Americas', 'CA': 'Americas', 'BR': 'Americas', 'MX': 'Americas',
    'AR': 'Americas', 'CL': 'Americas', 'CO': 'Americas',
    'GB': 'Europe', 'DE': 'Europe', 'FR': 'Europe', 'NL': 'Europe',
    'FI': 'Europe', 'SE': 'Europe', 'DK': 'Europe', 'NO': 'Europe',
    'ES': 'Europe', 'IT': 'Europe', 'CH': 'Europe', 'BE': 'Europe',
    'AT': 'Europe', 'PT': 'Europe', 'IE': 'Europe', 'PL': 'Europe',
    'CZ': 'Europe', 'HU': 'Europe', 'RO': 'Europe',
    'JP': 'Asia-Pacific', 'CN': 'Asia-Pacific', 'HK': 'Asia-Pacific',
    'KR': 'Asia-Pacific', 'AU': 'Asia-Pacific', 'SG': 'Asia-Pacific',
    'IN': 'Asia-Pacific', 'TW': 'Asia-Pacific', 'NZ': 'Asia-Pacific',
    'TH': 'Asia-Pacific', 'ID': 'Asia-Pacific', 'MY': 'Asia-Pacific',
}

ALL_REGIONS = ['Americas', 'Europe', 'Asia-Pacific', 'Other']

# Default region targets (%) — user-overridable via user_settings.region_targets
DEFAULT_REGION_TARGETS = {
    'Americas':     50,
    'Europe':       35,
    'Asia-Pacific': 10,
    'Other':         5,
}


def country_to_region(country_code: str) -> str:
    """Map ISO-2 country code to canonical region name."""
    if not country_code:
        return 'Other'
    return REGION_MAP.get(country_code.strip().upper(), 'Other')
