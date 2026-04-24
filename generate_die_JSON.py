from image_processing import save_die_engrave_data
from onshape_client import build_die_from_json

def main():
    json_path = "die_output.json"

    save_die_engrave_data(
        output_path=json_path,
        face1="handcuffs.png",
        face2="bird-scepter.png",
        die_size=0.7
    )

    build_die_from_json(
        json_path=json_path,
        output_file="engraved_die.stl"
    )

if __name__ == "__main__":
    main()