# Releasing and publishing

Steps that need the repository owner. Done for v0.1.x on 11 Nov 2026: steps 1–3, and the hacs/default PR in step 4.

## 1. Public GitHub repository

1. Create `pietrouk/ha-glow-statistics` on GitHub as a **public** repository. If you use another
   name or owner, update `documentation`, `issue_tracker` and `codeowners` in
   `custom_components/glow_statistics/manifest.json` and the install URL in `README.md`.
2. Push this repository:

   ```bash
   git remote add origin git@github.com:pietrouk/ha-glow-statistics.git
   git push -u origin main
   ```

3. In the repository settings:
   * **Description**, for example: "Home Assistant integration: Hildebrand Glow (DCC) half-hourly
     gas and electricity as long-term statistics for the Energy dashboard".
   * **Topics**: `home-assistant`, `hacs`, `homeassistant-integration`, `hildebrand-glow`,
     `smart-meter`, `energy`.
   * **Issues**: enabled.
4. Check that the **Validate** (hassfest, HACS) and **Tests** workflows pass under *Actions*.

## 2. Tagged release

1. Set `version` in `manifest.json`, for example `0.1.0`.
2. Commit, then tag and publish a GitHub release. HACS installs from releases.

   ```bash
   git tag v0.1.0
   git push origin v0.1.0
   gh release create v0.1.0 --generate-notes
   ```

## 3. Brand icons

Since Home Assistant 2026.3 a custom integration ships its own icon in
`custom_components/glow_statistics/brand/` (`icon.png` 256×256, `icon@2x.png` 512×512).
They are drawn by `tools/make_icon.py` (`uv run --with pillow python tools/make_icon.py
custom_components/glow_statistics/brand`). The Marketplace reads them from the latest release.

## 4. HACS default repository (optional)

Until this step, people install it as a custom repository (see README).

1. Make sure the repository has a release, a description, topics, issues enabled, and passing
   hassfest and HACS actions (no `ignore`).
2. Fork [hacs/default](https://github.com/hacs/default) and add `pietrouk/ha-glow-statistics` to the
   `integration` file, in alphabetical order.
3. Open the PR and fill in its checklist. Review can take weeks.
