import argparse
import json
import random
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from app.config import DATA_DIR, ROOT, policy
from app.seed import rows
from app.seed.notes import AREA_CODES, VENDORS, ClaimFacts, claims_from_dataset

SCANS_DIR = DATA_DIR / "scans"
TRUTH_PATH = SCANS_DIR / "truth.jsonl"
FONT = DATA_DIR / "fonts" / "DejaVuSans.ttf"
BOLD = DATA_DIR / "fonts" / "DejaVuSans-Bold.ttf"
SCAN_SEED = 20260717
PER_REGION = 15
KINDS = ("invoice", "estimate", "proof_of_loss")
DPI = 200
PAGE = (int(8.5 * DPI), 11 * DPI)
MISMATCHES = ("transposed", "dropped-decimal") * 4
DEGRADATIONS = ("blur", "blur", "low-contrast", "skew", "skew", "speckle", "speckle", "smudge")

LINE_ITEMS = {
    "hail": [
        "Tear off and replace laminated shingles",
        "Ice and water shield at eaves and valleys",
        "Replace drip edge and step flashing",
        "Replace gutters and downspouts",
        "Replace dented roof vents",
        "Dumpster and haul-off",
    ],
    "wind": [
        "Replace missing shingles and underlayment",
        "Replace fence panels",
        "Replace vinyl siding, west elevation",
        "Remove fallen limb and debris",
        "Temporary tarp",
    ],
    "water": [
        "Water extraction and drying equipment",
        "Remove and replace drywall",
        "Replace base cabinets",
        "Replace luxury vinyl plank flooring",
        "Prime and paint affected rooms",
    ],
    "fire": [
        "Board-up and emergency services",
        "Smoke and soot cleaning",
        "Remove and replace drywall and insulation",
        "Contents cleaning and storage",
        "Replace kitchen cabinets and counters",
    ],
    "theft": [
        "Replace forced entry door and frame",
        "Rekey locks",
        "Replace broken window",
        "Repair garage door opener",
    ],
    "mold": [
        "Containment and negative air",
        "Remove affected drywall and vanity",
        "Antimicrobial treatment",
        "Post-remediation clearance testing",
    ],
}
CITIES = {
    "MN": ["Minneapolis", "St. Paul", "Bloomington"],
    "WI": ["Milwaukee", "Madison", "Waukesha"],
    "TX": ["Austin", "Round Rock", "San Marcos"],
    "OK": ["Oklahoma City", "Norman", "Edmond"],
    "PA": ["Philadelphia", "Lancaster", "Allentown"],
    "OH": ["Columbus", "Dublin", "Westerville"],
    "CO": ["Denver", "Aurora", "Lakewood"],
    "AZ": ["Phoenix", "Mesa", "Tempe"],
}


@dataclass(frozen=True)
class LineItem:
    description: str
    amount: str


@dataclass(frozen=True)
class ScanTruth:
    doc_id: str
    file: str
    claim_id: int
    region: str
    kind: str
    claim_number: str
    vendor: str
    date: str
    items: list[LineItem]
    # What the page shows, which is what OCR should read. It differs from ledger_total on a planted mismatch.
    total: str
    ledger_total: str
    mismatch: str | None
    degraded: str | None

    def fields(self) -> dict[str, str]:
        out = {"claim_number": self.claim_number, "vendor": self.vendor, "date": self.date, "total": self.total}
        out |= {f"line_{i}": f"{item.description} | {item.amount}" for i, item in enumerate(self.items, 1)}
        return out


def money(value: Decimal) -> str:
    return f"{value:,.2f}"


def plain(value: Decimal) -> str:
    return f"{value:.2f}"


def _split(rng: random.Random, total: Decimal, descriptions: list[str]) -> list[LineItem]:
    count = max(1, min(len(descriptions), int(total // 400), rng.randint(3, 6)))
    names = rng.sample(descriptions, count)
    weights = [rng.randint(10, 40) for _ in names]
    cents = int(total * 100)
    amounts = [cents * w // sum(weights) for w in weights]
    amounts[0] += cents - sum(amounts)
    return [LineItem(name, plain(Decimal(a) / 100)) for name, a in zip(names, amounts, strict=True)]


def _transpose(rng: random.Random, value: Decimal) -> Decimal:
    digits = list(f"{value:.2f}".replace(".", ""))
    # Swap two unequal neighbours in the dollars when there are any, so the page still looks plausible.
    pairs = [i for i in range(len(digits) - 3) if digits[i] != digits[i + 1]] or [
        i for i in range(len(digits) - 1) if digits[i] != digits[i + 1]
    ]
    i = rng.choice(pairs)
    digits[i], digits[i + 1] = digits[i + 1], digits[i]
    return Decimal("".join(digits)) / 100


def _dated(rng: random.Random, claim: ClaimFacts, kind: str) -> date:
    assert claim.closed_date is not None
    if kind == "estimate":
        return min(claim.closed_date, claim.reported_date + timedelta(days=rng.randint(2, 12)))
    return max(claim.reported_date + timedelta(days=1), claim.closed_date - timedelta(days=rng.randint(2, 15)))


def plan(claims: list[ClaimFacts]) -> list[ScanTruth]:
    """Which claims get which documents, and what each page says. Pure, so truth.jsonl can be regenerated."""
    rng = random.Random(SCAN_SEED)
    eligible = [c for c in claims if c.status == "closed" and c.paid_indemnity() >= 150]
    chosen: list[ClaimFacts] = []
    for region in policy()["regions"]:
        chosen += rng.sample([c for c in eligible if c.region == region], PER_REGION)
    chosen.sort(key=lambda c: c.claim_id)
    kinds = [KINDS[i % len(KINDS)] for i in range(len(chosen))]
    rng.shuffle(kinds)
    order = list(range(len(chosen)))
    rng.shuffle(order)
    mismatch = dict(zip(order[: len(MISMATCHES)], MISMATCHES, strict=True))
    degraded = dict(zip(order[len(MISMATCHES) : len(MISMATCHES) + len(DEGRADATIONS)], DEGRADATIONS, strict=True))

    plans = []
    for i, (claim, kind) in enumerate(zip(chosen, kinds, strict=True)):
        ledger = claim.paid_indemnity()
        items = [] if kind == "proof_of_loss" else _split(rng, ledger, LINE_ITEMS[claim.peril])
        shown = ledger
        if mismatch.get(i) == "transposed":
            shown = _transpose(rng, ledger)
        total = plain(shown) if mismatch.get(i) != "dropped-decimal" else f"{ledger:.2f}".replace(".", "")
        plans.append(
            ScanTruth(
                doc_id=f"scan-{claim.claim_id}-{kind.replace('_', '-')}",
                file=f"{claim.claim_id}-{kind.replace('_', '-')}.png",
                claim_id=claim.claim_id,
                region=claim.region,
                kind=kind,
                claim_number=str(claim.claim_id),
                vendor=rng.choice(VENDORS[claim.state]),
                date=_dated(rng, claim, kind).isoformat(),
                items=items,
                total=total,
                ledger_total=plain(ledger),
                mismatch=mismatch.get(i),
                degraded=degraded.get(i),
            )
        )
    return plans


# Where a piece of text was drawn: x, y, the text and its font.
Anchor = tuple[int, int, str, ImageFont.FreeTypeFont]


class Page:
    def __init__(self) -> None:
        self.image = Image.new("L", PAGE, 255)
        self.draw = ImageDraw.Draw(self.image)
        self.y = int(0.7 * DPI)
        self.left = int(0.8 * DPI)
        self.right = PAGE[0] - int(0.8 * DPI)
        self.body = ImageFont.truetype(str(FONT), round(11 * DPI / 72))
        self.bold = ImageFont.truetype(str(BOLD), round(11 * DPI / 72))
        self.title = ImageFont.truetype(str(BOLD), round(20 * DPI / 72))
        self.line = round(11 * DPI / 72 * 1.6)
        self.anchors: dict[str, Anchor] = {}

    def text(self, x: int, text: str, font: ImageFont.FreeTypeFont | None = None, key: str | None = None) -> None:
        font = font or self.body
        self.draw.text((x, self.y), text, fill=0, font=font)
        if key:
            self.anchors[key] = (x, self.y, text, font)

    def right_text(self, text: str, font: ImageFont.FreeTypeFont | None = None, key: str | None = None) -> None:
        font = font or self.body
        self.text(self.right - round(font.getlength(text)), text, font, key)

    def field(self, label: str, value: str) -> None:
        self.text(self.left, label, self.bold)
        self.text(self.left + int(2.1 * DPI), value)
        self.advance()

    def advance(self, lines: float = 1) -> None:
        self.y += round(self.line * lines)

    def rule(self) -> None:
        self.draw.line((self.left, self.y, self.right, self.y), fill=0, width=2)
        self.advance(0.4)


def _amount_text(truth: ScanTruth) -> str:
    return f"${money(Decimal(truth.total))}" if truth.mismatch != "dropped-decimal" else f"${truth.total}"


def render(truth: ScanTruth, claim: ClaimFacts) -> Page:
    rng = random.Random(f"{SCAN_SEED}:{truth.doc_id}")
    page = Page()
    city = rng.choice(CITIES[claim.state])
    vendor_phone = f"({AREA_CODES[claim.state]}) 555-01{rng.randint(0, 99):02d}"
    day = date.fromisoformat(truth.date)
    if truth.kind == "proof_of_loss":
        page.text(page.left, "SWORN STATEMENT IN PROOF OF LOSS", page.title)
        page.advance(2)
        page.field("Claim No.", truth.claim_number)
        page.field("Insured", claim.holder)
        page.field("Property", f"{city}, {claim.state}")
        page.field("Date of loss", f"{claim.loss_date:%m/%d/%Y}")
        page.field("Cause of loss", claim.peril.capitalize())
        page.field("Contractor", truth.vendor)
        page.advance()
        page.rule()
        paid = Decimal(truth.ledger_total)
        for label, value in (("Amount of loss", paid + claim.deductible), ("Less deductible", claim.deductible)):
            page.text(page.left, label)
            page.right_text(f"${money(value)}")
            page.advance()
        page.text(page.left, "Net amount claimed", page.bold)
        page.right_text(_amount_text(truth), page.bold, key="total")
        page.advance(2)
        for sentence in (
            "The insured states that the loss did not originate by any act or design of the insured,",
            "that nothing has been concealed from the insurer, and that the amount claimed is due.",
        ):
            page.text(page.left, sentence)
            page.advance()
        page.advance()
        page.field("Signed", claim.holder)
        page.field("Date signed", f"{day:%m/%d/%Y}")
        return page

    heading = "INVOICE" if truth.kind == "invoice" else "ESTIMATE"
    page.text(page.left, truth.vendor, page.title)
    page.right_text(heading, page.title)
    page.advance(1.4)
    page.text(page.left, f"{city}, {claim.state}   Phone {vendor_phone}")
    page.advance(2)
    initials = "".join(word[0] for word in truth.vendor.split() if word[0].isalpha())
    page.field(f"{heading.title()} No.", f"{initials}-{day:%y}-{rng.randint(1000, 9999)}")
    page.field(f"{heading.title()} date", f"{day:%m/%d/%Y}")
    page.field("Claim No.", truth.claim_number)
    page.field("Bill to" if truth.kind == "invoice" else "Owner", claim.holder)
    page.field("Property", f"{city}, {claim.state}")
    page.advance()
    page.text(page.left, "Description", page.bold)
    page.right_text("Amount", page.bold)
    page.advance()
    page.rule()
    for item in truth.items:
        page.text(page.left, item.description)
        page.right_text(f"${money(Decimal(item.amount))}")
        page.advance()
    page.rule()
    page.text(page.left + int(3.6 * DPI), "Total due" if truth.kind == "invoice" else "Estimate total", page.bold)
    page.right_text(_amount_text(truth), page.bold, key="total")
    page.advance(2.5)
    page.text(page.left, f"Remit to: {truth.vendor}, {city}, {claim.state}")
    page.advance()
    page.text(page.left, "Thank you for your business." if truth.kind == "invoice" else "Estimate valid for 30 days.")
    return page


def degrade(image: Image.Image, how: str, rng: random.Random, total_anchor: Anchor) -> Image.Image:
    if how == "blur":
        return image.filter(ImageFilter.GaussianBlur(radius=rng.uniform(2.4, 2.9)))
    if how == "low-contrast":
        # Ink at about 190 on paper at about 248: faint, as from a worn toner cartridge, but still there.
        return image.point(lambda v: round(248 - (255 - v) * 0.23))
    if how == "skew":
        return image.rotate(
            rng.choice((-1, 1)) * rng.uniform(3.5, 5.0), resample=Image.Resampling.BICUBIC, fillcolor=255
        )
    if how == "speckle":
        pixels = image.load()
        assert pixels is not None
        width, height = image.size
        for _ in range(int(width * height * 0.06)):
            x, y = rng.randrange(width), rng.randrange(height)
            pixels[x, y] = 0 if rng.random() < 0.5 else 255
        return image
    if how == "smudge":
        x, y, text, font = total_anchor
        dot = text.index(".")
        cx = x + round(font.getlength(text[:dot]) + font.getlength(".") / 2)
        cy = y + round(font.getbbox(text)[3] * 0.85)
        draw = ImageDraw.Draw(image)
        for _ in range(40):
            dx, dy, r = rng.uniform(-10, 10), rng.uniform(-8, 6), rng.uniform(6, 12)
            draw.ellipse((cx + dx - r, cy + dy - r, cx + dx + r, cy + dy + r), fill=rng.randint(60, 110))
        return image
    raise ValueError(how)


def truth_lines(plans: list[ScanTruth]) -> str:
    return "".join(json.dumps(asdict(p), sort_keys=True) + "\n" for p in plans)


def build(root: Path = SCANS_DIR, images: bool = True) -> list[ScanTruth]:
    claims = claims_from_dataset(rows.generate(as_of=policy()["as_of"]))
    by_id = {c.claim_id: c for c in claims}
    plans = plan(claims)
    root.mkdir(parents=True, exist_ok=True)
    (root / TRUTH_PATH.name).write_text(truth_lines(plans))
    if images:
        for truth in plans:
            page = render(truth, by_id[truth.claim_id])
            image = page.image
            if truth.degraded:
                rng = random.Random(f"{SCAN_SEED}:degrade:{truth.doc_id}")
                image = degrade(image, truth.degraded, rng, page.anchors["total"])
            image.save(root / truth.file, dpi=(DPI, DPI), optimize=True)
    return plans


def main() -> None:
    parser = argparse.ArgumentParser(description="Render the scanned invoices, estimates and proofs of loss.")
    parser.add_argument("--truth-only", action="store_true", help="rewrite truth.jsonl without touching the images")
    args = parser.parse_args()
    plans = build(images=not args.truth_only)
    counts = {kind: sum(p.kind == kind for p in plans) for kind in KINDS}
    print(f"{len(plans)} scans {counts} in {SCANS_DIR.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
