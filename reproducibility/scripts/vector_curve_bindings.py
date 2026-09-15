"""Hash-bound layout and legend decisions; no transcribed curve samples.

Legend/axis semantics independently reviewed in vector-semantic-20260907-01.
All coordinates are PDF points. FTIR zero anchors are inside the plotting
frame; its bottom is not zero. XRD's y coordinate is explicitly uncalibrated.
"""
PDF_SHA = "b7acfcbaf50338961c68db74f1baf1c64299ab58a4f30926394843c8c380cc03"


def axis(p0, v0, p1, v1, unit, scale="linear", semantics="reported_axis"):
    return {"anchors": [[p0, v0], [p1, v1]], "unit": unit, "scale": scale, "semantics": semantics}


def series(label, record_type, record_label, color, minimum=20):
    return {"label": label, "record_type": record_type, "record_label": record_label, "color": color, "min_path_items": minimum}


def specification():
    raw_psd = [series("FA", "mat", "fly ash", [.251, .502, .502], 1),
               series("Slag", "mat", "slag", [.502, .502, 0], 1),
               series("FGR", "mat", "flue gas residue", [.808, 0, .808], 1)]
    cumulative_psd = [dict(item) for item in raw_psd]
    cumulative_psd[2] = {**cumulative_psd[2], "color": [.843, 0, .843]}
    mixes = [series("FGR-0.24", "mix", "FGR-0.24", [0, 0, 0]),
             series("FGR-0.32", "mix", "FGR-0.32", [1, 0, 0]),
             series("FGR-0.40", "mix", "FGR-0.40", [0, 0, 1])]
    return {"schema_version": 1, "pdf_sha256": PDF_SHA, "semantic_review_message_id": "vector-semantic-20260907-01", "panels": [
        {"figure": "Fig. 1", "panel_label": "a", "figure_type": "PSD", "page": 3,
         "curve_type": "reported_volume_density", "plot_bbox": [141.280, 58.461, 281.549, 173.514],
         "exclude_boxes": [[250, 59, 281, 83]],
         "x_axis": axis(141.280, 1, 281.549, 1000, "um", "log10"),
         "y_axis": axis(173.514, 0, 58.461, 8, "%", semantics="volume_density_not_numerical_derivative"), "series": raw_psd},
        {"figure": "Fig. 1", "panel_label": "b", "figure_type": "PSD", "page": 3,
         "curve_type": "cumulative_finer", "plot_bbox": [316.6355, 57.9843, 461.6766, 174.0058],
         "exclude_boxes": [[429, 59, 461, 85]],
         "x_axis": axis(316.6355, 1, 461.6766, 1000, "um", "log10"),
         "y_axis": axis(174.0058, 0, 57.9843, 100, "%"), "series": cumulative_psd},
        {"figure": "Fig. 3", "panel_label": "a", "figure_type": "FTIR", "page": 4,
         "curve_type": "absorbance_arbitrary_units", "plot_bbox": [151.2027, 590.1595, 288.6529, 705.4056],
         "x_axis": axis(151.2027, 1600, 288.6529, 400, "cm-1"),
         "y_axis": axis(698.200, 0, 590.1595, .15, "a.u.", semantics="absorbance_arbitrary_units_no_transmittance_conversion"),
         "calibration_uncertainty_pt": .3,
         "series": [series("GGBFS", "mat", "slag", [0, 0, 0]), series("FA", "mat", "fly ash", [0, 0, 1])]},
        {"figure": "Fig. 3", "panel_label": "b", "figure_type": "FTIR", "page": 4,
         "curve_type": "absorbance_arbitrary_units", "plot_bbox": [318.1338, 589.6175, 463.795, 706.7889],
         "x_axis": axis(318.1338, 1600, 463.795, 400, "cm-1"),
         "y_axis": axis(696.1292, 0, 589.6175, .1, "a.u.", semantics="absorbance_arbitrary_units_no_transmittance_conversion"), "series": mixes},
        {"figure": "Fig. 4", "panel_label": "a", "figure_type": "XRD_QXRD", "page": 5,
         "curve_type": "xrd_offset_traces", "plot_bbox": [133.294, 55.716, 362.216, 246.458],
         "x_axis": axis(133.294, 10, 362.216, 65, "degree_2theta"),
         "y_axis": axis(246.458, 0, 55.716, 1, "plot_fraction", semantics="uncalibrated_display_height_offsets_preserved_not_intensity"),
         "series": [series("FGR", "mat", "flue gas residue", [0, 0, 0]), series("FA", "mat", "fly ash", [1, 0, 0]), series("Slag", "mat", "slag", [0, 0, 1])]},
        {"figure": "Fig. 4", "panel_label": "b", "figure_type": "XRD_QXRD", "page": 5,
         "curve_type": "xrd_offset_traces", "plot_bbox": [131.017, 280.259, 376.368, 479.403],
         "x_axis": axis(131.017, 10, 376.261, 70, "degree_2theta"),
         "y_axis": axis(479.403, 0, 280.259, 1, "plot_fraction", semantics="uncalibrated_display_height_offsets_preserved_not_intensity"), "series": mixes},
    ]}
