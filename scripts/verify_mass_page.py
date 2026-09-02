"""Headless-Check der 📈-Seite: Run fl500 wählen, Seite öffnen, Fehler melden.

Einmalige Verifikation nach UI-Änderungen — kein Teil der Pipeline.
Aufruf: .venv/bin/python scripts/verify_mass_page.py
"""
from pathlib import Path

from playwright.sync_api import sync_playwright

SHOT = Path("data/tmp/mass_page.png")


def main() -> None:
    SHOT.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        br = p.chromium.launch()
        pg = br.new_page(viewport={"width": 1500, "height": 1300})
        pg.goto("http://localhost:8501", wait_until="networkidle")
        pg.wait_for_timeout(2500)

        # Run umstellen: die Optionsliste ist virtualisiert → tippen statt klicken.
        pg.get_by_role("combobox").first.click()
        pg.wait_for_timeout(400)
        pg.keyboard.type("fl500")
        pg.wait_for_timeout(800)
        pg.keyboard.press("Enter")
        pg.wait_for_timeout(4000)

        pg.get_by_text("📈 Massenauswertung", exact=False).first.click()
        pg.wait_for_timeout(9000)

        body = pg.inner_text("body")
        fehler = [z for z in body.splitlines()
                  if "Traceback" in z or "Error" in z or "Exception" in z]
        print("FEHLER:" if fehler else "keine Fehlermeldung im DOM")
        for z in fehler[:10]:
            print("  ", z[:160])

        for marke in ["Massenauswertung", "Physische DB-Schäden", "Nach Schadensgröße",
                      "Nach Erfassungsquelle", "Pro Auto"]:
            print(f"  {'✓' if marke in body else '✗'} {marke}")

        pg.screenshot(path=str(SHOT), full_page=True)
        print("Screenshot:", SHOT)
        br.close()


if __name__ == "__main__":
    main()
