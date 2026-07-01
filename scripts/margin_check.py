from __future__ import annotations

import argparse


def money(value: float) -> str:
    return f"${value:.4f}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Estimate Apify Actor per-minute margin.")
    parser.add_argument("--price", type=float, default=0.10, help="Customer price per transcription minute.")
    parser.add_argument("--provider-cost", type=float, default=0.006, help="Provider cost per audio minute.")
    parser.add_argument("--cu-price", type=float, default=0.20, help="Apify compute unit price.")
    parser.add_argument("--memory-gb", type=float, default=2.0, help="Actor memory in GB.")
    parser.add_argument("--runtime-ratio", type=float, default=2.0, help="Wall-clock runtime divided by source audio duration.")
    parser.add_argument("--pass-through", action="store_true", help="Platform usage is paid by the user separately.")
    args = parser.parse_args()

    after_apify_share = args.price * 0.8
    platform_cost = 0.0 if args.pass_through else args.memory_gb * args.runtime_ratio * args.cu_price / 60.0
    profit = after_apify_share - args.provider_cost - platform_cost
    margin = profit / args.price if args.price else 0.0
    break_even_runtime_ratio = ((after_apify_share - args.provider_cost) * 60.0) / (args.memory_gb * args.cu_price)

    print(f"price_per_minute={money(args.price)}")
    print(f"net_after_apify_share={money(after_apify_share)}")
    print(f"provider_cost_per_minute={money(args.provider_cost)}")
    print(f"platform_cost_per_minute={money(platform_cost)}")
    print(f"profit_per_minute={money(profit)}")
    print(f"margin={margin:.1%}")
    if not args.pass_through:
        print(f"break_even_runtime_ratio={break_even_runtime_ratio:.1f}x")
    if profit <= 0:
        raise SystemExit("margin check failed: price is at or below break-even")


if __name__ == "__main__":
    main()
