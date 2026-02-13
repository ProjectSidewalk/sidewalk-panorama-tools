SELECT pano_data.pano_id, source, pano_x, pano_y, label_type_id, camera_heading, heading, pitch, label.label_id
FROM label_point
INNER JOIN label
INNER JOIN pano_data ON label.pano_id = pano_data.pano_id
ON label.label_id = label_point.label_id;
