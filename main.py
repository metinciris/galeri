import os
import shutil
import logging
from flask import Flask, request, render_template, jsonify
from dotenv import load_dotenv
import pyvips
from git import Repo

app = Flask(__name__)

# Setup logging
logging.basicConfig(filename='app.log', level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

load_dotenv()

GITHUB_USERNAME = os.getenv('GITHUB_USERNAME')
GITHUB_TOKEN = os.getenv('GITHUB_TOKEN')  # Add this to your .env: GITHUB_TOKEN=your_token_with_repo_scope
GALLERY_REPO_NAME = 'galeri'
LOCAL_REPO_BASE = os.getenv('LOCAL_REPO_BASE', 'repos')

# Ensure directories exist
os.makedirs('uploads', exist_ok=True)
os.makedirs('output', exist_ok=True)
os.makedirs(LOCAL_REPO_BASE, exist_ok=True)

@app.route('/')
def index():
    # List existing repos in LOCAL_REPO_BASE
    repos = [d for d in os.listdir(LOCAL_REPO_BASE) if os.path.isdir(os.path.join(LOCAL_REPO_BASE, d)) and d != GALLERY_REPO_NAME]
    return render_template('index.html', repos=repos)

@app.route('/upload', methods=['POST'])
def upload():
    try:
        svs_file = request.files.get('svs_file')
        thumbnail_file = request.files.get('thumbnail')
        slide_name = request.form.get('slide_name')
        description = request.form.get('description', '')
        repo_name = request.form.get('repo')

        if not svs_file or svs_file.filename == '':
            raise ValueError('No SVS file selected')
        if not svs_file.filename.lower().endswith('.svs'):
            raise ValueError('File must be .svs')
        if not slide_name:
            slide_name = svs_file.filename.rsplit('.', 1)[0]
        if not repo_name:
            raise ValueError('No repository selected')

        logging.info(f'Starting processing for {slide_name} in repo {repo_name}')

        # Save uploaded files temporarily
        input_path = os.path.join('uploads', svs_file.filename)
        svs_file.save(input_path)
        thumbnail_path = None
        if thumbnail_file and thumbnail_file.filename != '':
            thumbnail_path = os.path.join('uploads', thumbnail_file.filename)
            thumbnail_file.save(thumbnail_path)
        logging.info(f'Files saved temporarily')

        # Prepare output directory for DZI
        output_dir = os.path.join('output', slide_name)
        os.makedirs(output_dir, exist_ok=True)

        # Convert with pyvips
        image = pyvips.Image.new_from_file(input_path, access='sequential')
        dzi_path = os.path.join(output_dir, 'slide')
        image.dzsave(dzi_path)
        logging.info(f'DeepZoom conversion completed for {slide_name}')

        # Local repo path
        repo_path = os.path.join(LOCAL_REPO_BASE, repo_name)
        if not os.path.exists(repo_path):
            raise ValueError(f'Repo path {repo_path} does not exist. Please clone it first.')

        repo = Repo(repo_path)

        # Copy DZI and tiles to repo
        shutil.copy(os.path.join(output_dir, 'slide.dzi'), os.path.join(repo_path, 'slide.dzi'))
        if os.path.exists(os.path.join(output_dir, 'slide_files')):
            dst_files = os.path.join(repo_path, 'slide_files')
            if os.path.exists(dst_files):
                shutil.rmtree(dst_files)
            shutil.copytree(os.path.join(output_dir, 'slide_files'), dst_files)

        # Create viewer index.html
        viewer_html = f"""
<!DOCTYPE html>
<html>
<head>
    <title>{slide_name}</title>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/openseadragon/4.1.0/openseadragon.min.js"></script>
    <style>
        body {{ margin: 0; }}
        #openseadragon {{ width: 100vw; height: 100vh; }}
    </style>
</head>
<body>
    <div id="openseadragon"></div>
    <script>
        var viewer = OpenSeadragon({{
            id: "openseadragon",
            prefixUrl: "https://cdnjs.cloudflare.com/ajax/libs/openseadragon/4.1.0/images/",
            tileSources: "slide.dzi",
            showNavigator: true
        }});
    </script>
</body>
</html>
        """
        with open(os.path.join(repo_path, 'index.html'), 'w') as f:
            f.write(viewer_html)
        logging.info(f'Viewer HTML created for {slide_name}')

        # Copy thumbnail if provided
        if thumbnail_path:
            shutil.copy(thumbnail_path, os.path.join(repo_path, 'thumbnail.jpg'))
            logging.info(f'Thumbnail copied to repo')

        # Create README.md
        pages_link = f'https://{GITHUB_USERNAME}.github.io/{repo_name}/'
        readme_content = f"""
# {slide_name}

{description}

"""
        if thumbnail_path:
            readme_content += "![Thumbnail](thumbnail.jpg)\n\n"
        readme_content += f"View the slide at [{pages_link}]({pages_link})\n"
        
        with open(os.path.join(repo_path, 'README.md'), 'w') as f:
            f.write(readme_content)
        logging.info(f'README.md created/updated')

        # Git add and commit
        repo.git.add(all=True)
        repo.index.commit(f'Added/Updated slide {slide_name}')
        logging.info(f'Files committed to local repo')

        # Push to GitHub
        remote_url = f'https://{GITHUB_USERNAME}:{GITHUB_TOKEN}@github.com/{GITHUB_USERNAME}/{repo_name}.git'
        if 'origin' not in [r.name for r in repo.remotes]:
            repo.create_remote('origin', remote_url)
        repo.remotes.origin.push()
        logging.info(f'Pushed to GitHub repo: {repo_name}')

        # Update gallery repo with index.html
        gallery_path = os.path.join(LOCAL_REPO_BASE, GALLERY_REPO_NAME)
        if not os.path.exists(gallery_path):
            gallery_clone_url = f'https://{GITHUB_USERNAME}:{GITHUB_TOKEN}@github.com/{GITHUB_USERNAME}/{GALLERY_REPO_NAME}.git'
            Repo.clone_from(gallery_clone_url, gallery_path)
            logging.info(f'Gallery repo cloned to {gallery_path}')
        gallery_repo = Repo(gallery_path)

        # Manage gallery index.html
        index_path = os.path.join(gallery_path, 'index.html')
        slides = []
        if os.path.exists(index_path):
            with open(index_path, 'r', encoding='utf-8') as f:
                content = f.read()
                # Simple parse: assume list items between <ul> and </ul>
                start = content.find('<ul>')
                end = content.find('</ul>')
                if start != -1 and end != -1:
                    list_content = content[start+4:end]
                    slides = [line.strip()[4:-5] for line in list_content.split('\n') if line.strip().startswith('<li>')]

        # Check if slide already exists
        if any(slide_name in s for s in slides):
            logging.info(f'Slide {slide_name} already in gallery, updating...')
            # Remove old entry
            slides = [s for s in slides if slide_name not in s]

        # Add new entry
        entry = f'<li><a href="{pages_link}">{slide_name}</a> - {description}'
        if thumbnail_path:
            entry += f' <img src="{pages_link}thumbnail.jpg" alt="Thumbnail" width="100">'
        entry += '</li>'
        slides.append(entry)

        # Create new index.html
        gallery_html = f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Slide Gallery</title>
</head>
<body>
    <h1>Slide Gallery</h1>
    <ul>
        {"\n        ".join(slides)}
    </ul>
</body>
</html>
        """
        with open(index_path, 'w', encoding='utf-8') as f:
            f.write(gallery_html)

        gallery_repo.git.add('index.html')
        gallery_repo.index.commit(f'Added/Updated link for {slide_name} in gallery')
        gallery_repo.remotes.origin.push()
        logging.info(f'Gallery index.html updated with link to {slide_name}')

        # Cleanup
        os.remove(input_path)
        if thumbnail_path:
            os.remove(thumbnail_path)
        shutil.rmtree(output_dir)
        logging.info(f'Cleanup completed for {slide_name}')

        return jsonify({'success': True, 'message': f'Slide {slide_name} processed and uploaded to {repo_name} successfully. View at {pages_link}. Gallery updated at https://{GITHUB_USERNAME}.github.io/{GALLERY_REPO_NAME}/'})
    
    except Exception as e:
        logging.error(f'Error processing upload: {str(e)}')
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/status')
def status():
    return render_template('status.html')

if __name__ == '__main__':
    app.run(debug=True)