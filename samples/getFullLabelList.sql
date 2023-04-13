SELECT gsv_data.gsv_panorama_id, pano_x, pano_y, label_type_id, camera_heading, heading, pitch, label.label_id
FROM label_point
INNER JOIN label
INNER JOIN gsv_data ON label.gsv_panorama_id = gsv_data.gsv_panorama_id
ON label.label_id = label_point.label_id;
