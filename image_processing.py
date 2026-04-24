import json
from pathlib import Path
from typing import Optional, Literal

import cv2


def build_die_engrave_data(
    face1: Optional[str] = None,
    face2: Optional[str] = None,
    face3: Optional[str] = None,
    face4: Optional[str] = None,
    face5: Optional[str] = None,
    face6: Optional[str] = None,
    die_size: float = 0.7,
    usable_face: Optional[float] = None,
) -> dict:
    """
    Build engraving data for up to six die faces.

    Parameters
    ----------
    face1..face6:
        Paths to image files. None skips that face.
    die_size:
        Die size in inches.
    usable_face:
        Optional explicit usable planar face width in inches.
        If None, defaults to the older 0.55/0.7 scale rule.
    """
    if usable_face is None:
        usable_face = die_size * (0.55 / 0.7)

    engrave_depth = 0.035 * die_size/0.7

    return {
        "die_size_in": die_size,
        "usable_face_in": usable_face,
        "faces": {
            "face1": _process_face(face1, usable_face, engrave_depth),
            "face2": _process_face(face2, usable_face, engrave_depth),
            "face3": _process_face(face3, usable_face, engrave_depth),
            "face4": _process_face(face4, usable_face, engrave_depth),
            "face5": _process_face(face5, usable_face, engrave_depth),
            "face6": _process_face(face6, usable_face, engrave_depth),
        },
    }


def _process_face(
    image_path: Optional[str],
    usable_face: float,
    engrave_depth: float,
) -> Optional[dict]:
    """
    Convert one image into engraveable polygon regions.

    Returns only the black-filled regions that should actually be cut.
    Each region is represented as:
        {
            "outer": [(x, y), ...],
            "holes": [[(x, y), ...], ...]
        }

    Contour hierarchy rule:
    - even depth  => black region
    - odd depth   => hole inside parent black region
    """
    if image_path is None:
        return None

    img = cv2.imread(image_path, cv2.IMREAD_GRAYSCALE)
    assert img is not None, f"Could not read image: {image_path}"

    cutoff = 100
    _, mask = cv2.threshold(img, cutoff, 255, cv2.THRESH_BINARY_INV)

    contours, hierarchy = cv2.findContours(
        mask, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE
    )
    assert hierarchy is not None, f"No contour hierarchy found in image: {image_path}"

    hierarchy = hierarchy[0]  # shape: (n, 4), entries are [next, prev, first_child, parent]

    min_area = 50

    # Keep simplified polygons per contour index.
    simplified: dict[int, list[tuple[float, float]]] = {}

    for i, cnt in enumerate(contours):
        area = cv2.contourArea(cnt)
        if area < min_area:
            continue

        epsilon = 0.002 * cv2.arcLength(cnt, True)
        approx = cv2.approxPolyDP(cnt, epsilon, True)

        pts = [(float(p[0][0]), float(p[0][1])) for p in approx]
        if len(pts) >= 3:
            simplified[i] = pts

    assert simplified, f"No valid loops found in image: {image_path}"

    def contour_depth(idx: int) -> int:
        """
        Return nesting depth in the contour tree.
        Top-level contour has depth 0.
        """
        depth = 0
        parent = hierarchy[idx][3]
        while parent != -1:
            depth += 1
            parent = hierarchy[parent][3]
        return depth

    def immediate_children(idx: int) -> list[int]:
        """
        Return all immediate child contour indices of idx.
        """
        children = []
        child = hierarchy[idx][2]  # first_child
        while child != -1:
            children.append(child)
            child = hierarchy[child][0]  # next sibling
        return children

    # Build black regions:
    # every even-depth contour becomes an engrave region
    # its immediate odd-depth children become holes
    raw_regions = []

    for idx in simplified:
        depth = contour_depth(idx)
        if depth % 2 != 0:
            continue  # odd depth = white hole, not an engrave region

        outer = simplified[idx]
        holes = []

        for child_idx in immediate_children(idx):
            if child_idx in simplified:
                # Immediate children of an even-depth contour are odd-depth holes.
                holes.append(simplified[child_idx])

        raw_regions.append({
            "outer": outer,
            "holes": holes,
        })

    assert raw_regions, f"No engraveable regions found in image: {image_path}"

    # Compute bounds from all region geometry we are actually keeping.
    all_x = []
    all_y = []

    for region in raw_regions:
        for x, y in region["outer"]:
            all_x.append(x)
            all_y.append(y)
        for hole in region["holes"]:
            for x, y in hole:
                all_x.append(x)
                all_y.append(y)

    min_x, max_x = min(all_x), max(all_x)
    min_y, max_y = min(all_y), max(all_y)

    img_w = max_x - min_x
    img_h = max_y - min_y
    longest = max(img_w, img_h)

    assert longest > 0, f"Degenerate image bounds for image: {image_path}"

    scale = usable_face / longest

    def scale_loop(loop: list[tuple[float, float]]) -> list[tuple[float, float]]:
        new_loop = []
        for x, y in loop:
            x2 = (x - (min_x + img_w / 2.0)) * scale
            y2 = ((min_y + img_h / 2.0) - y) * scale
            new_loop.append((x2, y2))
        return new_loop

    scaled_regions = []
    for region in raw_regions:
        scaled_regions.append({
            "outer": scale_loop(region["outer"]),
            "holes": [scale_loop(hole) for hole in region["holes"]],
        })

    return {
        "engrave_depth_in": engrave_depth,
        "regions": scaled_regions,
    }


def save_die_engrave_data(output_path: str | Path, **kwargs) -> dict:
    """
    Save engraving data to a local JSON file.
    """
    data = build_die_engrave_data(**kwargs)
    output_path = Path(output_path)
    output_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data