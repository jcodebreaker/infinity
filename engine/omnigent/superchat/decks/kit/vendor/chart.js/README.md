# Vendored: Chart.js 4.4.3

`chart.umd.js` is `dist/chart.umd.js` from the npm package `chart.js@4.4.3` (MIT, see
`LICENSE.md`), copied unchanged. The version is the one Open Design's chart templates load
(`html-ppt/templates/single-page/chart-*.html`, `cdn.jsdelivr.net/npm/chart.js@4.4.3`).

- npm integrity: `sha512-qK1gkGSRYcJzqrrzdR6a+I0vQ4/R+SoODXyAjscQ/4mzuNzySaMCd+hyVxitSY1+L2fjPD1Gbn+ibNqRmwQeLw==`
- git head: `3d0801299283cb6cb8e777e0cd45e6172cc43966`

`kit.build_deck` inlines it into every deck (`<script data-superchat-chartjs>`), because the viewer's
CSP and the offline host cannot load it from a CDN. To update, re-copy the file from the new
pinned version and bump the version and integrity above.
