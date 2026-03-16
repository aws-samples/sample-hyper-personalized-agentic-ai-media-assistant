import json
from moviepy.video.io.VideoFileClip import VideoFileClip
from PIL import Image, ImageDraw, ImageFont
import matplotlib.pyplot as plt
import io, base64
from IPython.display import display, HTML
import logging
from urllib.parse import urlparse
import math
from botocore.exceptions import ClientError
import datetime
import subprocess
from pathlib import Path
import glob
import uuid


logging.getLogger('moviepy').setLevel(logging.WARNING)

def read_json_on_s3(s3_uri, s3_client):
    # Parse s3 bucket and key from s3 uri
    s3_bucket = s3_uri.split('/')[2]
    s3_key = s3_uri.replace(f's3://{s3_bucket}/','')
    
    # Read BDA output_config file on S3
    response = s3_client.get_object(Bucket=s3_bucket, Key=s3_key)
    file_content = response['Body'].read().decode('utf-8')  # Read the content and decode it to a string
    # Convert the content to JSON
    return json.loads(file_content)

def delete_s3_folder(bucket_name, folder_prefix, s3_client):
    """
    Delete all objects within an S3 folder.

    :param bucket_name: Name of the S3 bucket
    :param folder_prefix: Folder path (prefix) to delete (must end with '/')
    """
    
    # List all objects with the given prefix (folder)
    response = s3_client.list_objects_v2(Bucket=bucket_name, Prefix=folder_prefix)
    
    if 'Contents' in response:
        objects_to_delete = [{'Key': obj['Key']} for obj in response['Contents']]
        
        # Delete all objects in the folder
        s3_client.delete_objects(Bucket=bucket_name, Delete={'Objects': objects_to_delete})
        print(f"Deleted folder: {folder_prefix} in bucket: {bucket_name}")
    else:
        print(f"No objects found in folder: {folder_prefix}")

def plot_text(sample_video_movie, result_data, chapter_index):
    # plot all frames with boundingbox in the given scene
    width = result_data["metadata"]["frame_width"]
    height = result_data["metadata"]["frame_height"]
    
    from moviepy.video.io.VideoFileClip import VideoFileClip
    from PIL import Image, ImageDraw
    import matplotlib.pyplot as plt
    
    with VideoFileClip(sample_video_movie) as video_clip:
        for chapter in result_data["chapters"]:
            if chapter["chapter_index"] == chapter_index:
                for frame in chapter["frames"]:
                    bboxes = []
                    if frame.get("logos"):
                        for tl in frame["logos"]:
                            for l in tl["locations"]:
                                bbox = l["bounding_box"]
                                if bbox:
                                    bboxes.append(
                                        {"bbox":(
                                                    width*bbox["left"], 
                                                    height*bbox["top"], 
                                                    width * (bbox["width"]+bbox["left"]), 
                                                    height * (bbox["height"] + bbox["top"])
                                                ),
                                         "text": f"{tl['name']}"})
                    if bboxes:
                        timestamp = frame["timestamp_millis"]/1000
                        frame = video_clip.get_frame(timestamp)  
                        frame_image = Image.fromarray(frame)
                        draw = ImageDraw.Draw(frame_image)
    
                        txts = []
                        for box in bboxes:
                            draw.rectangle(box["bbox"], outline="yellow", width=2)
                            if box["text"] not in txts:
                                txts.append(box['text'])
    
                        plt.figure(figsize=(10, 6))
                        plt.imshow(frame_image)
                        plt.title(f"At {timestamp} s, logo: {','.join(txts)}")
                        plt.axis("off")
                        plt.show()

def plot_logo(sample_video_movie, result_data, chapter_index):
    width = result_data["metadata"]["frame_width"]
    height = result_data["metadata"]["frame_height"]
    
    with VideoFileClip(sample_video_movie) as video_clip:
        for chapter in result_data["chapters"]:
            if chapter["chapter_index"] == chapter_index:
                for frame in chapter["frames"]:
                    bboxes = []
                    if frame.get("logos"):
                        for tl in frame["logos"]:
                            for l in tl["locations"]:
                                bbox = l["bounding_box"]
                                if bbox:
                                    bboxes.append(
                                        {"bbox":(
                                                    width*bbox["left"], 
                                                    height*bbox["top"], 
                                                    width * (bbox["width"]+bbox["left"]), 
                                                    height * (bbox["height"] + bbox["top"])
                                                ),
                                         "text": f"{tl['name']}"})
                    if bboxes:
                        timestamp = frame["timestamp_millis"]/1000
                        frame = video_clip.get_frame(timestamp)  
                        frame_image = Image.fromarray(frame)
                        draw = ImageDraw.Draw(frame_image)
    
                        txts = []
                        for box in bboxes:
                            draw.rectangle(box["bbox"], outline="yellow", width=2)
                            if box["text"] not in txts:
                                txts.append(box['text'])
    
                        plt.figure(figsize=(10, 6))
                        plt.imshow(frame_image)
                        plt.title(f"At {timestamp} s, logo: {','.join(txts)}")
                        plt.axis("off")
                        plt.show()

def generate_shot_images(sample_video_movie, result_data, image_width = 120):
    # Generate shot images
    width = result_data["metadata"]["frame_width"]
    height = result_data["metadata"]["frame_height"]
    
    images = []
    def find_chapter_by_shot_index(shot_index):
        # Loop through chapters and return the first match
        idx = 0
        for chapter in result_data["chapters"]:
            if shot_index and shot_index in chapter["shot_indices"]:
                chapter["chapter_index"] = idx
                return chapter
            idx += 1
        return None
    
    with VideoFileClip(sample_video_movie) as video_clip:
        chapter_index = None
        for shot in result_data["shots"]:
            frame = video_clip.get_frame(shot["start_timestamp_millis"]/1000)  
            image = Image.fromarray(frame)
            image = image.resize((image_width, int(image.height * image_width / image.width)))
            ci = find_chapter_by_shot_index(shot["shot_index"])
            if ci and chapter_index != ci["chapter_index"]:
                chapter_index = ci["chapter_index"]
            else:
                ci = None
            images.append({
                "image": image,
                "shot": f'shot {shot["shot_index"]}',
                "chapter": "" if ci is None else f"chapter {ci['chapter_index']}"
            })
    return images

def plot_shots(images):
    # HTML to display images in a table
    html_content = "<table style='border-collapse: collapse;'>"
    
    def pil_to_base64(pil_image):
        buffered = io.BytesIO()
        pil_image.save(buffered, format="PNG")
        return base64.b64encode(buffered.getvalue()).decode('utf-8')
    
    # Loop to create rows
    for i in range(0, len(images), 10):
        html_content += "<tr>"
        
        # Loop to add images and texts in each row
        for j in range(10):
            if i + j < len(images):
                html_content += f"""
                    <td style="padding: 1px; text-align: center;vertical-align:top;">
                        <div style="display:inline;position:relative;min-height:18px;border:solid 1px white;">{images[i + j]['chapter']}</div>
                        <img style="display:block" src="data:image/png;base64,{pil_to_base64(images[i + j]['image'])}" />
                        <div style="display:inline;position:relative;top:-20px; height:0px; color: white;">{images[i + j]['shot']}</div>
                    </td>
                """
        html_content += "</tr>"
    
    html_content += "</table>"
    
    # Display the HTML
    display(HTML(html_content))


def add_video_metadata_attributes(s3_client, s3_bucket, key, metadata):
    # Adding metadata attribute to allow filtering based on user query
    metadata_attributes = {
        "metadataAttributes": metadata
    } 
    metadata_key = f"{key}.metadata.json"
    s3_client.put_object(Bucket=s3_bucket, Key=metadata_key, Body=json.dumps(metadata_attributes))


def deduplicate_list_of_dicts(list_of_dicts):
    seen = set()
    new_list = []
    for d in list_of_dicts:
        t = tuple(sorted(d.items()))
        if t not in seen:
            seen.add(t)
            new_list.append(d)
    return new_list

def deduplicate_list_of_dicts(list_of_dicts):
    seen = set()
    new_list = []
    for d in list_of_dicts:
        t = tuple(sorted(d.items()))
        if t not in seen:
            seen.add(t)
            new_list.append(d)
    return new_list


def display_search_results(video, kb_response, max_results=5):
    Path("downloads").mkdir(parents=True, exist_ok=True)
    css_style = """
    <style>
        .video-container {
            display: flex;
            justify-content: space-around;
            flex-wrap: wrap;
        }
        .video {
            flex: 1;
            min-width: 200px;
            margin: 10px;
        }
        video {
            width: 100%;
            height: auto;
        }
    </style>
    """

    html_content = "<div class='video-container'>\n"
    text_response = kb_response["output"]["text"]
    print(text_response)
    video_count_idx = 0
    captured_citations = []
    if "citations" in kb_response:
        citations = kb_response["citations"]
        for idx, citation in enumerate(citations):
            if video_count_idx == max_results:
                break
            for retrieved_reference in citation["retrievedReferences"]:
                if video_count_idx == max_results:
                    break
                reference_metadata = retrieved_reference["metadata"]
                start_time = reference_metadata["start_timestamp_millis"] / 1000
                end_time = reference_metadata["end_timestamp_millis"] / 1000
                video_metadata = {}
                video_metadata["start_timestamp_millis"] = reference_metadata["start_timestamp_millis"]
                video_metadata["end_timestamp_millis"] = reference_metadata["end_timestamp_millis"]
                video_metadata["src_video"] = reference_metadata["src_media_s3_location"]
                captured_citations.append(video_metadata)
                video_count_idx += 1

    search_results = deduplicate_list_of_dicts(captured_citations)
    for idx, search_result in enumerate(search_results):
        start_time = search_result["start_timestamp_millis"] / 1000
        end_time = search_result["end_timestamp_millis"] / 1000
        random_id = str(uuid.uuid4())[-12:]
        citation_clip = f"downloads/citation-{random_id}.mp4"
        clip_video(video, citation_clip, start_time, end_time)
        html_content += f"""
        <div class="video">
            <h5>Result: {idx}, Time Range: {start_time} - {end_time} s</p>
            <video controls>
                <source src="{citation_clip}" type="video/mp4">
                Your browser does not support the video tag.
            </video>
        </div>
    """
    html_content += "</div>"
    
    display(HTML(css_style + html_content))


def display_chapter_videos(num_chapters=2):
            
    css_style = """
    <style>
        .video-container {
            display: flex;
            justify-content: space-around;
            flex-wrap: wrap;
        }
        .video {
            flex: 1;
            min-width: 200px;
            margin: 10px;
        }
        video {
            width: 100%;
            height: auto;
        }
    </style>
    """

    html_content = "<div class='video-container'>\n"
    for chapter_clip in glob.glob("outputs/chapter_videos/chapter*.mp4")[:num_chapters]:
        html_content += f"""
        <div class="video">
            <h5>Result: {chapter_clip}</p>
            <video controls>
                <source src="{chapter_clip}" type="video/mp4">
                Your browser does not support the video tag.
            </video>
        </div>
    """
    # render the shots
    html_content += "</div>"
    
    display(HTML(css_style + html_content))

def find_shots(result_data, start, end):
    shots_found = []
    for shot in result_data["shots"]:
        shot_start_time = shot["start_timestamp_millis"]
        if start <= shot_start_time and end >= shot_start_time:
            shots_found.append(shot)
    return shots_found

def find_key_frames(frames, start, end):
    segment_frames = []
    for frame in frames:
        if start <= frame["timestamp_millis"] and end >= frame["timestamp_millis"]:
            segment_frames.append(frame)
    max_frames_text = 5
    frame_distance = max(math.ceil(len(segment_frames) / max_frames_text), 1)
    segment_key_frames = []
    for key_frame_idx in range(0, len(segment_frames), frame_distance):
        segment_key_frame = {}
        segment_key_frame["timestamp_millis"] = segment_frames[key_frame_idx]["timestamp_millis"]
        segment_key_frames.append(segment_key_frame)
    return segment_key_frames

def detect_celebrities(rekognition_client, frame):
    celebrities = []
    try:
        img_byte_arr = io.BytesIO()
        frame.save(img_byte_arr, format='JPEG')
        img_bytes = img_byte_arr.getvalue()
        with open('test.jpg', 'wb') as f:
            f.write(img_bytes)

        # Call Rekognition API to detect celebrities
        response = rekognition_client.recognize_celebrities(
            Image={'Bytes': img_bytes}
        )
        min_confidence = 99.0 # change this value if the accuracy is low.

        for celebrity in response.get('CelebrityFaces', []):
            if celebrity.get('MatchConfidence', 0.0) >= min_confidence:
                celebrities.append(celebrity['Name'])
        return celebrities

    except ClientError as e:
        print(e)
        pass

def clip_video(video, output_file, video_start, video_end):
    converted_start = str(datetime.timedelta(seconds = video_start))
    converted_end = str(datetime.timedelta(seconds = video_end))
    result = subprocess.run(
            [
                "ffmpeg",
                "-ss",
                converted_start,
                "-to",
                converted_end,
                "-i",
                video, # path to video
                "-c",
                "copy",
                output_file,
                "-y"
            ],
            stdout=subprocess.DEVNULL, 
            stderr=subprocess.DEVNULL
        )    
    
def display_chapter_videos(video, chapter_details, num_chapters=2):
            
    css_style = """
    <style>
        .video-container {
            display: flex;
            justify-content: space-around;
            flex-wrap: wrap;
        }
        .video {
            flex: 1;
            min-width: 200px;
            margin: 10px;
        }
        video {
            width: 100%;
            height: auto;
        }
    </style>
    """

    html_content = "<div class='video-container'>\n"
    for idx, chapter in enumerate(chapter_details[:num_chapters]):
        start_time = chapter['start_timestamp_millis'] / 1000
        end_time = chapter['end_timestamp_millis'] / 1000
        chapter_clip_filepath = f"outputs/chapter_videos/chapter-{idx}-{chapter['start_timestamp_millis']}-{chapter['end_timestamp_millis']}.mp4"
        clip_video(video, chapter_clip_filepath, start_time, end_time)
        html_content += f"""
        <div class="video">
            <h5>Chapter: {idx}</p>
            <video controls>
                <source src="{chapter_clip_filepath}" type="video/mp4">
                Your browser does not support the video tag.
            </video>
        </div>
    """
    # render the shots
    html_content += "</div>"
    
    display(HTML(css_style + html_content))

def display_chapter_segment_videos(video, chapter_audio_segments, chapter_nbr, num_segments=3):
    
    chapter_segments = chapter_audio_segments[chapter_nbr]
    css_style = """
    <style>
        .video-container {
            display: flex;
            justify-content: space-around;
            flex-wrap: wrap;
        }
        .video {
            flex: 1;
            min-width: 200px;
            margin: 10px;
        }
        video {
            width: 100%;
            height: auto;
        }
    </style>
    """

    html_content = "<div class='video-container'>\n"
    for idx, chapter_segment in enumerate(chapter_segments[:num_segments]):
        start_time  = chapter_segment["start_timestamp_millis"] / 1000
        end_time  = chapter_segment["end_timestamp_millis"] / 1000
        chapter_segment_basename = f"chapter-{idx}-segment-{chapter_segment['start_timestamp_millis']}-{chapter_segment['end_timestamp_millis']}.txt"
        chapter_segment_clip_filepath = f"outputs/segment_videos/{Path(chapter_segment_basename).stem}.mp4"
        clip_video(video, chapter_segment_clip_filepath, start_time, end_time)
        html_content += f"""
        <div class="video">
            <h5>Chapter: {chapter_nbr}, Chapter Segment: {idx}</p>
            <video controls>
                <source src="{chapter_segment_clip_filepath}" type="video/mp4">
                Your browser does not support the video tag.
            </video>
        </div>
    """
    # render the shots
    html_content += "</div>"
    
    display(HTML(css_style + html_content))