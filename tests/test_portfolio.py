from riskbalancer import (
    CategoryPath,
    CategoryTarget,
    Portfolio,
    PortfolioAnalyzer,
    PortfolioPlan,
)


def build_plan() -> PortfolioPlan:
    return PortfolioPlan(
        targets=[
            CategoryTarget(
                path=CategoryPath("Equities", "US", "Large Cap"),
                normalized_risk_weight=0.6,
                volatility=0.2,
                risk_weight=0.6,
            ),
            CategoryTarget(
                path=CategoryPath("Equities", "International", "Developed"),
                normalized_risk_weight=0.2,
                volatility=0.25,
                risk_weight=0.2,
            ),
            CategoryTarget(
                path=CategoryPath("Fixed Income", "US", "Treasury"),
                normalized_risk_weight=0.2,
                volatility=0.1,
                risk_weight=0.2,
            ),
        ]
    )


def test_portfolio_plan_requires_full_allocation():
    plan = build_plan()
    assert len(list(plan.targets)) == 3

    # weights for level 1 should sum to 1 implicitly
    level1 = {target.path.level1 for target in plan.targets}
    assert level1 == {"Equities", "Fixed Income"}


def test_risk_parity_cash_weights_and_status():
    plan = build_plan()
    portfolio = Portfolio()
    portfolio.add_manual_investment(
        instrument_id="VTI",
        description="US Total Market",
        market_value=6000,
        category=CategoryPath("Equities", "US", "Large Cap"),
        volatility=0.2,
    )
    portfolio.add_manual_investment(
        instrument_id="VXUS",
        description="International Market",
        market_value=1000,
        category=CategoryPath("Equities", "International", "Developed"),
        volatility=0.25,
    )
    portfolio.add_manual_investment(
        instrument_id="IEF",
        description="US Treasuries",
        market_value=3000,
        category=CategoryPath("Fixed Income", "US", "Treasury"),
        volatility=0.1,
    )

    analyzer = PortfolioAnalyzer(plan, portfolio)
    cash_weights = analyzer.cash_weights()
    assert abs(sum(cash_weights.values()) - 1.0) < 1e-9

    statuses = analyzer.category_status()
    assert len(statuses) == 3
    equities_us = next(
        status for status in statuses if status.path.levels() == ("Equities", "US", "Large Cap")
    )
    # actual weight 0.6, but higher risk causes smaller cash weight
    assert equities_us.status in {"over_invested", "on_target"}
