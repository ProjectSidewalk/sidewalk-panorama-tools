select gsv_panorama_id, sv_image_x, sv_image_y, label_type_id, photographer_heading, heading, pitch, label.label_id from sidewalk.label_point
inner join sidewalk.label
on sidewalk.label.label_id = sidewalk.label_point.label_id
;
