"""Tax settings and configuration for portfolio analytics."""

from __future__ import annotations

from typing import Any


# Federal long-term capital gains tax brackets (2024)
LTCG_BRACKETS_2024 = {
    "single": [
        (44625, 0.0),      # 0% bracket
        (492300, 0.15),    # 15% bracket
        (float('inf'), 0.20),  # 20% bracket
    ],
    "married_filing_jointly": [
        (89250, 0.0),
        (553850, 0.15),
        (float('inf'), 0.20),
    ],
    "married_filing_separately": [
        (44625, 0.0),
        (276925, 0.15),
        (float('inf'), 0.20),
    ],
    "head_of_household": [
        (59550, 0.0),
        (523050, 0.15),
        (float('inf'), 0.20),
    ],
}

# State income tax rates on capital gains / long-term gains
# Reference: 2024 rates. Some states treat LTCG preferentially
STATE_TAX_RATES = {
    "AL": 0.0,      # Alabama - No special LTCG treatment
    "AK": 0.0,      # Alaska - No state income tax
    "AZ": 0.0549,   # Arizona
    "AR": 0.0,      # Arkansas - No special LTCG treatment
    "CA": 0.13,     # California - Treats LTCG as ordinary income
    "CO": 0.0475,   # Colorado
    "CT": 0.0,      # Connecticut - Tax on dividends/interest, not capital gains
    "DE": 0.0,      # Delaware - No tax on long-term capital gains
    "FL": 0.0,      # Florida - No state income tax
    "GA": 0.0575,   # Georgia
    "HI": 0.0,      # Hawaii - No capital gains tax for most
    "ID": 0.0585,   # Idaho
    "IL": 0.0,      # Illinois - No tax on capital gains
    "IN": 0.0,      # Indiana - No tax on capital gains
    "IA": 0.0,      # Iowa - No tax on capital gains
    "KS": 0.0,      # Kansas - No tax on capital gains
    "KY": 0.0,      # Kentucky - No tax on capital gains
    "LA": 0.0,      # Louisiana - No tax on capital gains
    "ME": 0.0,      # Maine - No tax on capital gains
    "MD": 0.0,      # Maryland - No tax on capital gains
    "MA": 0.0,      # Massachusetts - No tax on capital gains
    "MI": 0.0,      # Michigan - No tax on capital gains
    "MN": 0.0985,   # Minnesota
    "MS": 0.0,      # Mississippi - No tax on capital gains
    "MO": 0.0595,   # Missouri
    "MT": 0.0,      # Montana - No tax on capital gains
    "NE": 0.0684,   # Nebraska
    "NV": 0.0,      # Nevada - No state income tax
    "NH": 0.0,      # New Hampshire - No tax on capital gains
    "NJ": 0.0,      # New Jersey - No tax on long-term capital gains
    "NM": 0.0,      # New Mexico - No tax on capital gains
    "NY": 0.0685,   # New York
    "NC": 0.0475,   # North Carolina
    "ND": 0.0,      # North Dakota - No tax on capital gains
    "OH": 0.0,      # Ohio - No tax on capital gains
    "OK": 0.0,      # Oklahoma - No tax on capital gains
    "OR": 0.099,    # Oregon
    "PA": 0.05,     # Pennsylvania - Tax on capital gains
    "RI": 0.0,      # Rhode Island - No tax on capital gains
    "SC": 0.07,     # South Carolina
    "SD": 0.0,      # South Dakota - No state income tax
    "TN": 0.0,      # Tennessee - No state income tax
    "TX": 0.0,      # Texas - No state income tax
    "UT": 0.0,      # Utah - No tax on capital gains
    "VT": 0.0,      # Vermont - No tax on capital gains
    "VA": 0.0,      # Virginia - No tax on capital gains
    "WA": 0.0,      # Washington - No state income tax
    "WV": 0.0,      # West Virginia - No tax on capital gains
    "WI": 0.0,      # Wisconsin - No tax on capital gains
    "WY": 0.0,      # Wyoming - No state income tax
}

# Net Investment Income Tax (3.8%) applies to high-income taxpayers
NIIT_THRESHOLD_2024 = {
    "single": 200000,
    "married_filing_jointly": 250000,
    "married_filing_separately": 125000,
    "head_of_household": 200000,
}


class TaxSettings:
    """User tax configuration settings."""
    
    def __init__(self):
        # User profile
        self.filing_status: str = "single"  # single, married_filing_jointly, etc.
        self.agi: float = 100000.0  # Adjusted Gross Income
        self.state: str = "TX"  # Two-letter state code
        
        # Asset holding periods
        self.assume_long_term: bool = True  # Assume positions qualify for LTCG rates
        
    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict."""
        return {
            "filing_status": self.filing_status,
            "agi": self.agi,
            "state": self.state,
            "assume_long_term": self.assume_long_term,
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaxSettings:
        """Deserialize from dict."""
        settings = cls()
        settings.filing_status = data.get("filing_status", "single")
        settings.agi = float(data.get("agi", 100000.0))
        settings.state = data.get("state", "TX")
        settings.assume_long_term = data.get("assume_long_term", True)
        return settings


def get_federal_ltcg_rate(agi: float, filing_status: str = "single") -> float:
    """Get applicable federal LTCG tax rate based on AGI and filing status.
    
    Args:
        agi: Adjusted Gross Income
        filing_status: one of single, married_filing_jointly, married_filing_separately, head_of_household
    
    Returns:
        Tax rate as decimal (e.g., 0.15 for 15%)
    """
    brackets = LTCG_BRACKETS_2024.get(filing_status, LTCG_BRACKETS_2024["single"])
    
    for threshold, rate in brackets:
        if agi < threshold:
            return rate
    
    return brackets[-1][1]  # Highest rate


def get_state_ltcg_rate(state: str) -> float:
    """Get state capital gains tax rate.
    
    Args:
        state: Two-letter state code
    
    Returns:
        Tax rate as decimal (e.g., 0.05 for 5%)
    """
    return STATE_TAX_RATES.get(state.upper(), 0.0)


def apply_niit(agi: float, gains: float, filing_status: str = "single") -> float:
    """Calculate Net Investment Income Tax (3.8% Medicare tax).
    
    Applies to high-income taxpayers. For simplicity, assumes capital gains are the only NIIT.
    
    Args:
        agi: Adjusted Gross Income (not including gains)
        gains: Realized capital gains
        filing_status: Filing status
    
    Returns:
        NIIT tax amount
    """
    threshold = NIIT_THRESHOLD_2024.get(filing_status, 200000)
    total_income = agi + gains
    
    if total_income > threshold:
        excess_income = total_income - threshold
        niit_base = min(excess_income, gains)  # NIIT limited to gain amount
        return niit_base * 0.038
    
    return 0.0


def calculate_total_tax_rate(agi: float, state: str, filing_status: str = "single", include_niit: bool = True) -> float:
    """Calculate combined federal + state capital gains tax rate.
    
    Args:
        agi: Adjusted Gross Income
        state: Two-letter state code
        filing_status: Filing status
        include_niit: Include 3.8% NIIT for high earners
    
    Returns:
        Combined tax rate as decimal
    """
    federal_rate = get_federal_ltcg_rate(agi, filing_status)
    state_rate = get_state_ltcg_rate(state)
    
    # Combined rate (not quite additive due to federal deduction, but close)
    combined = federal_rate + state_rate
    
    # Add 3.8% NIIT for high-income filers (simplified)
    if include_niit:
        threshold = NIIT_THRESHOLD_2024.get(filing_status, 200000)
        if agi > threshold:
            combined += 0.038
    
    return min(combined, 1.0)  # Cap at 100%


def format_tax_rate(rate: float) -> str:
    """Format tax rate as percentage string."""
    return f"{rate * 100:.1f}%"


def get_state_list() -> list[tuple[str, str]]:
    """Get list of (state_code, state_name) tuples."""
    state_names = {
        "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
        "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
        "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
        "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
        "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
        "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
        "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
        "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
        "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
        "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
        "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
        "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
        "WI": "Wisconsin", "WY": "Wyoming",
    }
    return [(code, name) for code, name in sorted(state_names.items(), key=lambda x: x[1])]


def get_no_income_tax_states() -> list[str]:
    """Get list of states with no income tax."""
    return [
        "AK",  # Alaska
        "FL",  # Florida
        "NV",  # Nevada
        "SD",  # South Dakota
        "TN",  # Tennessee
        "TX",  # Texas
        "WA",  # Washington
        "WY",  # Wyoming
    ]
