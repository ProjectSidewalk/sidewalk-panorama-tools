import os
import shutil
def get_immediate_subdirectories(a_dir):
    return [name for name in os.listdir(a_dir)
            if os.path.isdir(os.path.join(a_dir, name))]

def reformat_panos(path_to_originals, output_dir):
    print("Reformatting files in "+path_to_originals+" to "+output_dir)
    subdirs = get_immediate_subdirectories(path_to_originals)
    completed_count = 0
    for dirname in subdirs:
        print("Moving items for panorama "+dirname)
        # Get first two letters of panorama ID
        first_two = dirname[:2]
        # Check for folder named first_two in distination directory; create if needed
        destination_folder = os.path.join(output_dir, first_two)
        if not os.path.isdir(destination_folder):
            os.makedirs(destination_folder)

        # Look for and copy depth.txt file
        depth_file_orig = os.path.join(path_to_originals, dirname, "depth.txt")
        if os.path.exists(depth_file_orig):
            # Copy to output_dir/XX/<panoid>.depth.txt
            shutil.copy2(depth_file_orig, os.path.join(destination_folder, dirname+".depth.txt"))

        # Look for and copy depth.xml file
        xml_file_orig = os.path.join(path_to_originals, dirname, "depth.xml")
        if os.path.exists(xml_file_orig):
            # Copy to output_dir/XX/<panoid>.xml
            shutil.copy2(xml_file_orig, os.path.join(destination_folder, dirname+".xml"))

        # Look for and copy panorama image
        pano_jpeg_orig = os.path.join(path_to_originals, dirname, "images", "pano.jpg")
        if os.path.exists(pano_jpeg_orig):
            # Copy to output_dir/XX/<panoid>.jpg
            shutil.copy2(pano_jpeg_orig, os.path.join(destination_folder, dirname+".jpg"))

        completed_count += 1
        print("Completed "+str(completed_count))



reformat_panos("/mnt/umiacs/Dataset/2013_09_19 - Extended Google Street View panorama dataset/GSV", "/mnt/umiacs/Panoramas/tohme")