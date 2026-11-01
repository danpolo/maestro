# Goals — Recipe Box

## 1. Purpose

Recipe Box turns the recipes Dan saves from Instagram into a searchable cookbook on his phone. It exists because saved posts are impossible to find again when he is actually cooking.

## 2. Who uses it

* **Dan** — saves posts during the week and cooks from them at the weekend.
* **Dan's partner** — browses the cookbook to suggest what to cook.

## 3. Goals

### G1 — Every saved post lands in the cookbook
* **Target**: 95% of posts saved in the last 7 days appear as recipes within 1 hour.
* **Verified by**: Metric — import ratio on the weekly report, at least 0.95.

### G2 — Find a recipe in seconds
* **Target**: a search by ingredient returns the right recipe in the top 3 results.
* **Verified by**: Command — `pytest tests/acceptance/test_search.py`

### G3 — Usable in the kitchen
* **Target**: a recipe page is readable on a phone from arm's length, with no login.
* **Verified by**: Observation by Dan — he opens three recipes on his phone while cooking
  and reads each one without zooming.

## 4. Final product walk-through

1. Dan saves a post on Instagram on Tuesday.
2. On Saturday he opens Recipe Box on his phone, types "lemon", and sees the dish in the first three results.
3. He props the phone on the counter and cooks from the page without touching it.

## 5. Non-goals

* No meal planning or shopping lists.
* No sharing recipes with other users.

## 6. Priority order (when goals conflict)

G3 > G1 > G2
