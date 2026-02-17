"""
Dota odds parser with automatic vig removal.
Handles 3-way odds (team A / draw / team B) where draw resolves 50/50 on Kalshi.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class MatchOdds:
    """Match odds with automatic no-vig calculation.

    Handles 3-way markets where draw resolves to 50/50 on Kalshi.
    """
    team_a: str
    team_b: str
    odds_a: float  # decimal odds with vig
    odds_b: float  # decimal odds with vig
    odds_draw: Optional[float] = None  # draw odds (if 3-way market)

    @property
    def implied_a(self) -> float:
        """Raw implied probability for team A."""
        return 1 / self.odds_a

    @property
    def implied_b(self) -> float:
        """Raw implied probability for team B."""
        return 1 / self.odds_b

    @property
    def implied_draw(self) -> float:
        """Raw implied probability for draw."""
        return 1 / self.odds_draw if self.odds_draw else 0

    @property
    def total_implied(self) -> float:
        """Total implied probability (>1 means vig)."""
        return self.implied_a + self.implied_b + self.implied_draw

    @property
    def vig(self) -> float:
        """Vig/juice percentage."""
        return (self.total_implied - 1) * 100

    @property
    def novig_a_raw(self) -> float:
        """No-vig probability for team A win (excluding draw)."""
        return self.implied_a / self.total_implied

    @property
    def novig_b_raw(self) -> float:
        """No-vig probability for team B win (excluding draw)."""
        return self.implied_b / self.total_implied

    @property
    def novig_draw(self) -> float:
        """No-vig probability for draw."""
        return self.implied_draw / self.total_implied

    @property
    def novig_a(self) -> float:
        """No-vig probability for team A on Kalshi (draw splits 50/50)."""
        return self.novig_a_raw + (self.novig_draw / 2)

    @property
    def novig_b(self) -> float:
        """No-vig probability for team B on Kalshi (draw splits 50/50)."""
        return self.novig_b_raw + (self.novig_draw / 2)

    @property
    def theo_a(self) -> int:
        """No-vig theo for team A in cents (for Kalshi)."""
        return int(round(self.novig_a * 100))

    @property
    def theo_b(self) -> int:
        """No-vig theo for team B in cents (for Kalshi)."""
        return int(round(self.novig_b * 100))

    @property
    def fair_odds_a(self) -> float:
        """Fair decimal odds for team A (no vig, Kalshi-adjusted)."""
        return 1 / self.novig_a

    @property
    def fair_odds_b(self) -> float:
        """Fair decimal odds for team B (no vig, Kalshi-adjusted)."""
        return 1 / self.novig_b

    def __str__(self):
        draw_str = f" / draw {self.odds_draw:.2f}" if self.odds_draw else ""
        draw_pct = f" (draw: {self.novig_draw*100:.1f}%)" if self.odds_draw else ""
        return (
            f"{self.team_a} vs {self.team_b}\n"
            f"  Raw odds: {self.odds_a:.2f}{draw_str} / {self.odds_b:.2f} (vig: {self.vig:.1f}%){draw_pct}\n"
            f"  Kalshi:   {self.theo_a}c / {self.theo_b}c\n"
            f"  Fair:     {self.fair_odds_a:.2f} / {self.fair_odds_b:.2f}"
        )

    def for_dashboard(self) -> dict:
        """Return dict formatted for dashboard input."""
        return {
            "team_a": self.team_a,
            "team_b": self.team_b,
            "odds_a": round(self.fair_odds_a, 2),
            "odds_b": round(self.fair_odds_b, 2),
            "theo_a": self.theo_a,
            "theo_b": self.theo_b,
        }


# Pre-loaded matches from dotaodds.txt (format: team_a, team_b, odds_a, odds_b, odds_draw)
# Draw resolves 50/50 on Kalshi
MATCHES = [
    MatchOdds("Aurora Gaming", "Yakult Brothers", 1.85, 6.66, 2.32),
    MatchOdds("BB Team", "Team Yandex", 3.63, 2.93, 1.99),
    MatchOdds("PariVision", "Team Liquid", 3.70, 2.88, 1.99),
    MatchOdds("paiN Gaming", "OG", 8.07, 1.62, 2.60),
    MatchOdds("Falcons", "Team Spirit", 2.01, 5.89, 2.21),
    MatchOdds("Tundra Esports", "MOUZ", 2.96, 3.59, 1.99),
    MatchOdds("Natus Vincere", "GamerLegion", 1.58, 8.60, 2.66),
    MatchOdds("Xtreme", "Execration", 1.70, 7.63, 2.47),
    MatchOdds("GamerLegion", "Tundra Esports", 2.55, 4.38, 2.05),
    MatchOdds("Xtreme", "Natus Vincere", 3.54, 3.00, 1.99),
    MatchOdds("Team Spirit", "Execration", 1.99, 2.97, 3.57),
    MatchOdds("OG", "Yakult Brothers", 1.62, 8.66, 2.62),
    MatchOdds("Team Liquid", "Team Yandex", 2.00, 3.49, 3.11),
    MatchOdds("paiN Gaming", "Aurora Gaming", 7.47, 1.75, 2.47),
    MatchOdds("Falcons", "Natus Vincere", 2.22, 5.28, 2.13),
    MatchOdds("GamerLegion", "Team Spirit", 2.48, 4.46, 2.08),
    MatchOdds("Execration", "MOUZ", 2.84, 3.85, 2.01),
    MatchOdds("OG", "Team Yandex", 3.70, 2.94, 2.01),
    MatchOdds("PariVision", "BB Team", 2.55, 4.38, 2.05),
    MatchOdds("Team Liquid", "Yakult Brothers", 1.50, 9.72, 2.90),
    MatchOdds("Falcons", "Execration", 1.50, 9.87, 2.87),
    MatchOdds("Natus Vincere", "GamerLegion", 1.77, 7.47, 2.42),
    MatchOdds("Tundra Esports", "Xtreme", 2.05, 4.38, 2.55),
]


def find_match(team: str) -> list[MatchOdds]:
    """Find matches involving a team (fuzzy search)."""
    team = team.lower()
    return [m for m in MATCHES if team in m.team_a.lower() or team in m.team_b.lower()]


def print_all():
    """Print all matches with no-vig odds."""
    for m in MATCHES:
        print(m)
        print()


if __name__ == "__main__":
    print_all()
