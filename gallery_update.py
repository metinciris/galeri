import os
import shutil
import logging
import requests
import time
import re
import base64
from flask import Flask, request, render_template, jsonify
from dotenv import load_dotenv
import pyvips
from git import Repo

os.environ['VIPS_WARNING'] = '0'  # Suppress VIPS warnings

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024 * 1024  # 2 GB limit
logging.basicConfig(filename='app.log', level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

load_dotenv()

GITHUB_USERNAME = os.getenv('GITHUB_USERNAME')
GITHUB_TOKEN = os.getenv('GITHUB_TOKEN')
GALLERY_REPO_NAME = 'galeri'
LOCAL_REPO_BASE = r'C:\slide-uploader\repos'

os.makedirs('uploads', exist_ok=True)
os.makedirs('output', exist_ok=True)
os.makedirs(LOCAL_REPO_BASE, exist_ok=True)

def get_repos():
    """Get sorted list of gallery repositories and suggest the next one."""
    repos = [d for d in os.listdir(LOCAL_REPO_BASE) if os.path.isdir(os.path.join(LOCAL_REPO_BASE, d)) and d != GALLERY_REPO_NAME and d.startswith('gallery-')]
    repos.sort()
    new_repo_suggestion = None
    if repos:
        repo_numbers = [int(re.match(r'gallery-(\d+)', repo).group(1)) for repo in repos if re.match(r'gallery-(\d+)', repo)]
        new_repo_suggestion = f'gallery-{max(repo_numbers) + 1:02d}' if repo_numbers else 'gallery-01'
    else:
        new_repo_suggestion = 'gallery-01'
    return repos, new_repo_suggestion

@app.route('/')
def index():
    repos, new_repo_suggestion = get_repos()
    return render_template('index.html', repos=repos, new_repo_suggestion=new_repo_suggestion)

@app.route('/upload', methods=['POST'])
def upload():
    summary = []
    try:
        svs_file = request.files.get('svs_file')
        thumbnail_file = request.files.get('thumbnail')
        slide_name = request.form.get('slide_name')
        description = request.form.get('description', '')
        repo_name = request.form.get('repo')
        repo_action = request.form.get('repo_action', 'use_existing')
        skip_tiles_upload = request.form.get('skip_tiles') == 'on'

        logging.info(f'Form data: slide_name={slide_name}, repo={repo_name}, repo_action={repo_action}, skip_tiles_upload={skip_tiles_upload}')
        summary.append(f"Processing slide: {slide_name}, repo: {repo_name}, repo_action: {repo_action}, skip_tiles_upload: {skip_tiles_upload}")

        if not slide_name:
            raise ValueError('Slide name is required')
        if not repo_name:
            raise ValueError('Repository not selected')
        if not svs_file or svs_file.filename == '':
            raise ValueError('SVS file not selected')
        if not svs_file.filename.lower().endswith('.svs'):
            raise ValueError('File must have .svs extension')

        input_path = os.path.join('uploads', svs_file.filename)
        svs_file.save(input_path)
        thumbnail_path = None
        if thumbnail_file and thumbnail_file.filename != '':
            thumbnail_path = os.path.join('uploads', thumbnail_file.filename)
            thumbnail_file.save(thumbnail_path)
        logging.info('Files temporarily saved')
        summary.append("Files temporarily saved")

        output_dir = os.path.join('output', slide_name)
        os.makedirs(output_dir, exist_ok=True)

        # Generate tiles locally
        image = pyvips.Image.new_from_file(input_path, access='sequential')
        dzi_path = os.path.join(output_dir, 'slide')
        image.dzsave(dzi_path)
        del image
        logging.info(f'DeepZoom conversion completed: {slide_name}')
        summary.append(f"DeepZoom conversion completed: {slide_name}")

        headers = {
            'Authorization': f'token {GITHUB_TOKEN}',
            'Accept': 'application/vnd.github+json'
        }
        repo_check_url = f'https://api.github.com/repos/{GITHUB_USERNAME}/{repo_name}'
        check_response = requests.get(repo_check_url, headers=headers)
        time.sleep(1)
        is_new_repo = check_response.status_code == 404

        if repo_action == 'create_new' and is_new_repo:
            data = {
                'name': repo_name,
                'description': f'Virtual microscopy for {slide_name}',
                'private': False,
                'auto_init': False
            }
            create_response = requests.post('https://api.github.com/user/repos', headers=headers, json=data)
            time.sleep(1)
            if create_response.status_code != 201:
                raise Exception(f'Failed to create repo: {create_response.json().get("message", "Unknown error")}')
            logging.info(f'GitHub repo created: {repo_name}')
            summary.append(f"GitHub repo created: {repo_name}")

            # Enable GitHub Pages
            pages_data = {
                'source': {
                    'branch': 'main',
                    'path': '/'
                }
            }
            pages_response = requests.post(f'https://api.github.com/repos/{GITHUB_USERNAME}/{repo_name}/pages', headers=headers, json=pages_data)
            time.sleep(1)
            if pages_response.status_code != 201:
                logging.warning(f'Failed to enable GitHub Pages: {pages_response.json().get("message", "Unknown error")}')
                summary.append(f"Failed to enable GitHub Pages (non-critical): {pages_response.json().get('message', 'Unknown error')}")
            else:
                logging.info(f'GitHub Pages enabled for {repo_name}')
                summary.append(f"GitHub Pages enabled for {repo_name}")
        elif is_new_repo and repo_action == 'use_existing':
            raise Exception(f'Repo {repo_name} does not exist and creation was not selected')
        elif check_response.status_code != 200:
            raise Exception(f'Failed to check repo: {check_response.json().get("message", "Unknown error")}')

        repo_path = os.path.join(LOCAL_REPO_BASE, repo_name)
        os.makedirs(repo_path, exist_ok=True)
        git_dir = os.path.join(repo_path, '.git')
        repo = None
        if not os.path.exists(git_dir):
            repo = Repo.init(repo_path)
            logging.info(f'Local repo initialized: {repo_path}')
            summary.append(f"Local repo initialized: {repo_path}")
        else:
            repo = Repo(repo_path)
            try:
                repo.git.fetch('origin', 'main')
                repo.git.reset('--hard', 'origin/main')
                logging.info(f'Local repo synced with remote: {repo_name}')
                summary.append(f"Local repo synced with remote: {repo_name}")
            except Exception as e:
                logging.warning(f'Failed to sync local repo {repo_name}: {str(e)}')
                summary.append(f"Failed to sync local repo {repo_name} (non-critical): {str(e)}")

        # Copy tiles to local repo regardless of skip_tiles_upload
        shutil.copy(os.path.join(output_dir, 'slide.dzi'), os.path.join(repo_path, 'slide.dzi'))
        if os.path.exists(os.path.join(output_dir, 'slide_files')):
            dst_files = os.path.join(repo_path, 'slide_files')
            if os.path.exists(dst_files):
                shutil.rmtree(dst_files)
            shutil.copytree(os.path.join(output_dir, 'slide_files'), dst_files)
        logging.info('Tiles copied to local repo')
        summary.append("Tiles copied to local repo")

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
            showNavigator: false
        }});
    </script>
</body>
</html>
        """
        with open(os.path.join(repo_path, 'index.html'), 'w', encoding='utf-8') as f:
            f.write(viewer_html)
        logging.info(f'Viewer HTML created: {slide_name}')
        summary.append(f"Viewer HTML created: {slide_name}")

        if thumbnail_path:
            shutil.copy(thumbnail_path, os.path.join(repo_path, 'thumbnail.jpg'))
            logging.info('Thumbnail copied to repo')
            summary.append("Thumbnail copied to repo")

        pages_link = f'https://{GITHUB_USERNAME}.github.io/{repo_name}/'
        readme_content = f"""
# {slide_name}

{description}

"""
        if thumbnail_path:
            readme_content += "![Thumbnail](thumbnail.jpg)\n\n"
        readme_content += f"View the slide at [{pages_link}]({pages_link})\n"
        if skip_tiles_upload:
            readme_content += "\n**Note:** Tiles are generated locally and included in the repository. Manually commit and push to GitHub to make the viewer work.\n"
        
        with open(os.path.join(repo_path, 'README.md'), 'w', encoding='utf-8') as f:
            f.write(readme_content)
        logging.info('README.md created/updated')
        summary.append("README.md created/updated")

        repo.git.add(all=True)
        repo.index.commit(f'Slide added/updated: {slide_name}')
        logging.info('Files committed to local repo')
        summary.append("Files committed to local repo")

        logging.info(f'Repository prepared at {repo_path} for manual upload via GitHub Desktop')
        summary.append(f"Repository prepared at {repo_path} for manual upload via GitHub Desktop")
        if skip_tiles_upload:
            summary.append(f"**Important:** Tiles included in {repo_path}. Manually commit and push to GitHub to avoid upload limits.")

        gallery_path = os.path.join(LOCAL_REPO_BASE, GALLERY_REPO_NAME)
        if not os.path.exists(gallery_path):
            gallery_clone_url = f'https://{GITHUB_USERNAME}:{GITHUB_TOKEN}@github.com/{GITHUB_USERNAME}/{GALLERY_REPO_NAME}.git'
            try:
                Repo.clone_from(gallery_clone_url, gallery_path)
                logging.info(f'Gallery repo cloned: {gallery_path}')
                summary.append(f"Gallery repo cloned: {gallery_path}")
            except Exception as e:
                logging.warning(f'Failed to clone gallery repo: {str(e)}')
                summary.append(f"Failed to clone gallery repo (non-critical): {str(e)}")

        gallery_repo = Repo(gallery_path)
        try:
            gallery_repo.git.fetch('origin', 'main')
            gallery_repo.git.reset('--hard', 'origin/main')
            logging.info('Gallery repo synced with remote')
            summary.append("Gallery repo synced with remote")
        except Exception as e:
            logging.warning(f'Failed to sync gallery repo: {str(e)}')
            summary.append(f"Failed to sync gallery repo (non-critical): {str(e)}")

        index_path = os.path.join(gallery_path, 'index.html')
        content = ''
        existing_links = set()
        slides = []
        start_content = 0
        if os.path.exists(index_path):
            with open(index_path, 'r', encoding='utf-8') as f:
                content = f.read()
            start = content.find('<ul id="sortable"')
            end = content.find('</ul>')
            if start != -1 and end != -1:
                start_content = content.find('>', start) + 1
                list_content = content[start_content:end]
                li_items = re.findall(r'<li>.*?</li>', list_content, re.DOTALL)
                for li in li_items:
                    match = re.search(r'<a href="https://[^/]+\.github\.io/([^/]+)/"', li)
                    if match:
                        existing_links.add(match.group(1))
                    slides.append(li.strip())

        if repo_name not in existing_links:
            clean_slide_name = ' '.join(word.capitalize() for word in slide_name.replace('gallery-', '').split('-'))
            description_text = description if description else 'No description available'
            if skip_tiles_upload:
                description_text += ' (Tiles pending manual upload)'
            thumbnail = f'<img src="{pages_link}thumbnail.jpg" alt="Thumbnail" class="w-full h-64 object-contain rounded-lg mb-4">' if thumbnail_path else ''
            entry = f'<li class="gallery-item bg-white p-6 rounded-xl shadow-lg"><a href="{pages_link}" class="block"><div>{thumbnail}<h2 class="text-xl font-semibold text-blue-600 hover:underline">{clean_slide_name}</h2><p class="text-gray-600 mt-2 text-sm">{description_text}</p></div></a></li>'
            slides.append(entry)
            logging.info('Added new entry to gallery index.html')
            summary.append("Added new entry to gallery index.html")

            gallery_html = f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Slide Gallery</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        .gallery-item {{ transition: transform 0.3s ease, box-shadow 0.3s ease; }}
        .gallery-item:hover {{ transform: scale(1.03); box-shadow: 0 12px 20px -4px rgba(0, 0, 0, 0.15); }}
        .gallery-item img {{ transition: transform 0.3s ease; }}
        .gallery-item:hover img {{ transform: scale(1.08); }}
        body {{ background: linear-gradient(to bottom, #f3f4f6, #e5e7eb); }}
    </style>
</head>
<body>
    <div class="container mx-auto px-4 sm:px-6 lg:px-8 py-12">
        <h1 class="text-4xl sm:text-5xl font-extrabold text-center text-gray-900 mb-12">Slide Gallery</h1>
        <ul id="sortable" class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-10">
        {'\n        '.join(slides)}
        </ul>
        <footer class="mt-12 text-center text-gray-500 text-sm">
            Last updated: {time.strftime('%Y-%m-%d %H:%M:%S')}
        </footer>
    </div>
</body>
</html>
            """
            with open(index_path, 'w', encoding='utf-8') as f:
                f.write(gallery_html)

            gallery_repo.git.add('index.html')
            gallery_repo.index.commit(f'Added link for {slide_name} to gallery')
            logging.info('Gallery index.html updated locally')
            summary.append("Gallery index.html updated locally")

        readme_path = os.path.join(gallery_path, 'README.md')
        readme_content = '# Slayt Galerisi\n\n'
        repos = [d for d in os.listdir(LOCAL_REPO_BASE) if d.startswith('gallery-') and os.path.isdir(os.path.join(LOCAL_REPO_BASE, d))]
        repos.sort()

        for r_name in repos:
            r_pages_link = f'https://{GITHUB_USERNAME}.github.io/{r_name}/'
            r_readme_path = os.path.join(LOCAL_REPO_BASE, r_name, 'README.md')
            r_description = 'No description available'
            r_slide_name = r_name
            if os.path.exists(r_readme_path):
                with open(r_readme_path, 'r', encoding='utf-8') as f:
                    r_readme_content = f.read()
                    lines = r_readme_content.split('\n')
                    r_slide_name = lines[0].strip('# ').strip() if lines and lines[0].startswith('#') else r_name
                    r_description = ' '.join([line.strip() for line in lines[1:] if line.strip() and not line.startswith('View the slide') and not line.startswith('![Thumbnail]') and not line.startswith('**Note:**')]) or 'No description available'
            readme_content += f'- [{r_slide_name}]({r_pages_link}) - {r_description}\n'

        with open(readme_path, 'w', encoding='utf-8') as f:
            f.write(readme_content)

        gallery_repo.git.add('README.md')
        gallery_repo.index.commit('README updated with all slides')
        logging.info('Gallery README.md updated locally')
        summary.append("Gallery README.md updated locally")

        logging.info("=== Process Summary ===")
        for item in summary:
            logging.info(item)
        logging.info("Python finished")

        return jsonify({
            'success': True,
            'message': f'Slide {slide_name} prepared in {repo_path} with all files. Gallery updated in {gallery_path}. Commit and push via GitHub Desktop.'
        })

    except Exception as e:
        logging.error(f'Upload error: {str(e)}')
        summary.append(f"Upload error: {str(e)}")
        logging.info("=== Process Summary ===")
        for item in summary:
            logging.info(item)
        logging.info("Python finished")
        return jsonify({'success': False, 'message': f'Error: {str(e)}'}), 500

@app.route('/status')
def status():
    return render_template('status.html')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
