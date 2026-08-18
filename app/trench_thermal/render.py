"""Renders the trench cross-section SVG from real solver output: actual
conduit positions, actual backfill envelope (rectangle or polygon), and a
continuous colour scale driven by each conduit's actual solved temperature.

Returns the SVG as a string so a caller can put it straight into an API
response; `out_path` is optional and only writes a copy to disk.
"""

from __future__ import annotations

from xml.sax.saxutils import escape as _xml_escape

M_TO_IN = 39.3701


def _lerp(a, b, t):
    return a + (b - a) * t


def _hex_to_rgb(h):
    h = h.lstrip('#')
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _rgb_to_hex(rgb):
    return '#%02X%02X%02X' % tuple(max(0, min(255, round(c))) for c in rgb)


def temp_to_color(t, t_min, t_max):
    """3-stop gradient: cool blue -> orange -> red, as t goes from t_min to t_max."""
    frac = 0.0 if t_max <= t_min else max(0.0, min(1.0, (t - t_min) / (t_max - t_min)))
    c_blue = _hex_to_rgb('#5B9BD5')
    c_orange = _hex_to_rgb('#E8912D')
    c_red = _hex_to_rgb('#C0392B')
    if frac < 0.5:
        f = frac / 0.5
        rgb = [_lerp(c_blue[i], c_orange[i], f) for i in range(3)]
    else:
        f = (frac - 0.5) / 0.5
        rgb = [_lerp(c_orange[i], c_red[i], f) for i in range(3)]
    return _rgb_to_hex(rgb)


def render_cross_section(conduits, T_cond, env, T_target_C, dx, dy,
                         out_path=None, scale_px_per_m=420, title=None,
                         backfill_polygon=None, subtitle=None):
    margin_left, margin_top = 170, 90
    x0_m = env['x_min'] - 0.35
    y0_m = -0.15  # start just above grade

    def px(x_m):
        return margin_left + (x_m - x0_m) * scale_px_per_m

    def py(y_m):
        return margin_top + (y_m - y0_m) * scale_px_per_m

    width_px = px(env['x_max'] + 0.35) + 40
    height_px = py(env['y_max'] + 0.35) + 120

    # The scale must always span the solved temperatures, not just up to the
    # target: on a FAILING trench every conductor sits above the target, and a
    # scale that topped out there would render the whole hot design in the
    # coolest colour and read as passing.
    t_min = min(T_cond.values())
    t_hot = max(T_cond.values())
    t_max = max(T_target_C, t_hot)
    over_limit = t_hot > T_target_C

    parts = []
    parts.append(f'<svg viewBox="0 0 {width_px:.0f} {height_px:.0f}" '
                 f'xmlns="http://www.w3.org/2000/svg" font-family="Arial, Helvetica, sans-serif">')
    parts.append('<defs><pattern id="soilHatch" width="10" height="10" '
                 'patternTransform="rotate(45)" patternUnits="userSpaceOnUse">'
                 '<rect width="10" height="10" fill="#C9A876"/>'
                 '<line x1="0" y1="0" x2="0" y2="10" stroke="#9C7C4E" stroke-width="2"/></pattern>'
                 '<marker id="arrow" viewBox="0 0 10 10" refX="5" refY="5" markerWidth="6" '
                 'markerHeight="6" orient="auto-start-reverse">'
                 '<path d="M0,0 L10,5 L0,10 z" fill="#333333"/></marker></defs>')

    parts.append(f'<rect x="0" y="0" width="{width_px:.0f}" height="{py(0):.0f}" fill="#EAF4FB"/>')
    parts.append(f'<rect x="0" y="{py(0):.0f}" width="{width_px:.0f}" '
                 f'height="{height_px - py(0):.0f}" fill="url(#soilHatch)"/>')
    parts.append(f'<line x1="0" y1="{py(0):.0f}" x2="{width_px:.0f}" y2="{py(0):.0f}" '
                 f'stroke="#2B2B2B" stroke-width="3"/>')
    parts.append(f'<text x="12" y="{py(0)-12:.0f}" font-size="15" fill="#2B2B2B">Finished Grade</text>')

    if title:
        parts.append(f'<text x="{width_px/2:.0f}" y="30" font-size="18" font-weight="bold" '
                     f'fill="#1A1A1A" text-anchor="middle">{_xml_escape(title)}</text>')
    if subtitle is None:
        spacing = f'{dx*M_TO_IN:.1f}in'
        if dy:
            spacing += f' x {dy*M_TO_IN:.1f}in'
        subtitle = f'Solved layout — {len(conduits)} conduits, spacing {spacing} O.C.'
    parts.append(f'<text x="{width_px/2:.0f}" y="52" font-size="12" fill="#555" '
                 f'text-anchor="middle" font-style="italic">{_xml_escape(subtitle)}</text>')

    # Backfill envelope: real polygon when one was solved, rectangle otherwise.
    if backfill_polygon:
        pts = ' '.join(f'{px(vx):.1f},{py(vy):.1f}' for vx, vy in backfill_polygon)
        parts.append(f'<polygon points="{pts}" fill="#EAD9B8" stroke="#8B6F47" '
                     f'stroke-width="2" stroke-dasharray="7,4"/>')
        label_x, label_y = px(min(v[0] for v in backfill_polygon)), py(min(v[1] for v in backfill_polygon)) - 10
    else:
        ex0, ey0 = px(env['x_min']), py(env['y_min'])
        ex1, ey1 = px(env['x_max']), py(env['y_max'])
        parts.append(f'<rect x="{ex0:.1f}" y="{ey0:.1f}" width="{ex1-ex0:.1f}" height="{ey1-ey0:.1f}" '
                     f'rx="6" fill="#EAD9B8" stroke="#8B6F47" stroke-width="2" stroke-dasharray="7,4"/>')
        label_x, label_y = ex0, ey0 - 10
    parts.append(f'<text x="{label_x:.1f}" y="{label_y:.1f}" font-size="13" fill="#6B4F2A">'
                 f'Engineered Backfill</text>')

    for c in conduits:
        cx, cy = px(c.x), py(c.y)
        r_px = c.r_duct * scale_px_per_m   # per-conduit: a schedule can mix trade sizes
        color = temp_to_color(T_cond[c.id], t_min, t_max)
        # A conduit over the limit gets a heavy dark ring on top of its colour,
        # so "this one is out of compliance" survives greyscale printing.
        exceeds = T_cond[c.id] > T_target_C
        stroke = '#5A0F0F' if exceeds else '#333'
        stroke_w = 3.5 if exceeds else 1.5
        parts.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r_px:.1f}" '
                     f'fill="{color}" stroke="{stroke}" stroke-width="{stroke_w}"/>')
        parts.append(f'<text x="{cx:.1f}" y="{cy+4:.1f}" font-size="10" fill="#fff" '
                     f'text-anchor="middle">{T_cond[c.id]:.0f}&#176;</text>')
        parts.append(f'<text x="{cx:.1f}" y="{cy - r_px - 6:.1f}" font-size="10" fill="#333" '
                     f'text-anchor="middle">{_xml_escape(c.id)}</text>')

    # Depth of cover dimension — to the top of the shallowest conduit.
    shallowest = min(conduits, key=lambda c: c.y - c.r_duct)
    top_y_m = shallowest.y - shallowest.r_duct
    dim_x = px(env['x_min']) - 60
    parts.append(f'<line x1="{dim_x:.1f}" y1="{py(0):.1f}" x2="{dim_x:.1f}" y2="{py(top_y_m):.1f}" '
                 f'stroke="#333" stroke-width="1.5" marker-start="url(#arrow)" marker-end="url(#arrow)"/>')
    mid_y = (py(0) + py(top_y_m)) / 2
    parts.append(f'<text x="{dim_x-18:.1f}" y="{mid_y:.1f}" font-size="12" fill="#333" '
                 f'transform="rotate(-90 {dim_x-18:.1f} {mid_y:.1f})" text-anchor="middle">'
                 f'Depth of Cover {top_y_m*M_TO_IN:.0f}in</text>')

    # Legend: continuous scale bar
    lg_x, lg_y, lg_w, lg_h = margin_left, height_px - 55, 260, 16
    steps = 40
    for s in range(steps):
        t = t_min + (t_max - t_min) * s / (steps - 1)
        color = temp_to_color(t, t_min, t_max)
        parts.append(f'<rect x="{lg_x + s*lg_w/steps:.1f}" y="{lg_y:.1f}" '
                     f'width="{lg_w/steps + 0.5:.1f}" height="{lg_h:.1f}" fill="{color}"/>')
    parts.append(f'<rect x="{lg_x:.1f}" y="{lg_y:.1f}" width="{lg_w:.1f}" height="{lg_h:.1f}" '
                 f'fill="none" stroke="#333" stroke-width="1"/>')
    parts.append(f'<text x="{lg_x:.1f}" y="{lg_y+lg_h+16:.1f}" font-size="11" fill="#333">'
                 f'{t_min:.0f}&#176;C (coolest conduit)</text>')
    parts.append(f'<text x="{lg_x+lg_w:.1f}" y="{lg_y+lg_h+16:.1f}" font-size="11" fill="#333" '
                 f'text-anchor="end">{t_max:.0f}&#176;C (hottest conduit)</text>')

    # Tick the target limit on the scale bar, wherever it falls — that is what
    # tells a reader at a glance which side of the limit the design landed on.
    if t_max > t_min:
        tick_frac = max(0.0, min(1.0, (T_target_C - t_min) / (t_max - t_min)))
        tick_x = lg_x + tick_frac * lg_w
        parts.append(f'<line x1="{tick_x:.1f}" y1="{lg_y-6:.1f}" x2="{tick_x:.1f}" '
                     f'y2="{lg_y+lg_h+3:.1f}" stroke="#111" stroke-width="2"/>')
        parts.append(f'<text x="{tick_x:.1f}" y="{lg_y-10:.1f}" font-size="10" fill="#111" '
                     f'text-anchor="middle">{T_target_C:.0f}&#176;C limit</text>')

    if over_limit:
        parts.append(f'<text x="{lg_x + lg_w + 20:.1f}" y="{lg_y+lg_h-2:.1f}" font-size="13" '
                     f'font-weight="bold" fill="#8E1B1B">EXCEEDS {T_target_C:.0f}&#176;C '
                     f'CONDUCTOR LIMIT — {t_hot:.0f}&#176;C</text>')

    parts.append(f'<text x="{width_px-15:.0f}" y="{height_px-12:.0f}" font-size="11" fill="#888" '
                 f'text-anchor="end" font-style="italic">Solver output — not to scale for print</text>')

    parts.append('</svg>')
    svg = '\n'.join(parts)
    if out_path:
        with open(out_path, 'w', encoding='utf-8') as f:
            f.write(svg)
    return svg
