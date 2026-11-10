"""Seed a directory reviewer's test account with fictional sample material.

The Claude Connectors Directory and ChatGPT plugin reviewers sign in to a test account and run
the prompts in integrations/chatgpt/plugin/plugin.json against it; this gives them something
to find.

Run with a developer key from the test account (Connect -> developer key, reaching ONE
Memory, with write access and "Your profile" ticked):

    MEMBASE_API_KEY=mbk_... uv run --with 'unibaseio-membase>=0.4' python scripts/seed_reviewer_account.py

Safe to re-run: documents are keyed by custom_id and profile facts are deduplicated.
Everything here is invented (persona "Alex Rivera", "Northwind Studio").
"""

import time

from membase import Membase

PROFILE = [
    "My name is Alex Rivera.",
    "I am the founder and lead product designer at Northwind Studio, a small design studio.",
    "I prefer short, direct answers with a clear recommendation first.",
    "I work in Lisbon (WET timezone) and keep Fridays free of meetings.",
    "I write in British English.",
]

DOCS = [
    (
        "website-redesign-status",
        "Website redesign: status notes",
        """Website redesign for northwindstudio.example — status as of the last working session.

Done:
- New homepage hero and navigation approved by the team.
- Case study template finished; two case studies migrated (Harbour Coffee, Atlas Bikes).

In progress / where I left off:
- Pricing page: layout drafted, copy not written yet. Waiting on the final Pro plan price.
- Contact form: needs spam protection before launch.

Next steps:
1. Write the pricing page copy once the Pro plan price is confirmed.
2. Migrate the remaining three case studies.
3. Accessibility pass (colour contrast on the dark theme failed two checks).
Target launch: end of next month.""",
    ),
    (
        "pricing-decision-pro-plan",
        "Pricing meeting notes: Pro plan",
        """Meeting notes — pricing review with Sam (finance) and Priya (product).

Decision: the Pro plan will cost $24 per month, or $240 per year (two months free).
Why: $29 tested poorly with freelancers in the survey; $19 would not cover support costs.
The Starter plan stays free with up to 3 projects.
Open question: whether to offer a student discount — revisit next quarter.
Owner for the pricing page copy: Alex.""",
    ),
    (
        "brand-guidelines",
        "Brand guidelines summary",
        """Northwind Studio brand guidelines (summary).
Primary colour: deep teal #0F5257. Accent: warm sand #E9D8A6.
Typeface: Inter for UI, Fraunces for headlines.
Tone of voice: friendly, plain English, no jargon; short sentences.
Logo: keep clear space equal to the height of the N on all sides.""",
    ),
]


def main() -> None:
    m = Membase()
    print("connected:", str(m.capabilities())[:300])

    for fact in PROFILE:
        r = m.profile_facts.add(fact)
        print("profile:", "added" if r.get("created", True) else "already there", "-", fact)

    for custom_id, title, text in DOCS:
        r = m.add(text, title=title, custom_id=custom_id)
        print("document:", title, "->", r.get("id"), r.get("status", ""))

    print("waiting for the memory to learn the documents (can take a few minutes)...")
    for _ in range(30):
        time.sleep(20)
        hits = m.search("What did we decide about the Pro plan price?", limit=3).get("items", [])
        if hits:
            print("search works; top hit:", str(hits[0])[:200])
            return
    print("not searchable yet; check again later with m.search(...) or in the Membase app.")


if __name__ == "__main__":
    main()
