from bs4 import BeautifulSoup

def extract_text(html: str) -> str:
    html_content = html 
    soup = BeautifulSoup(html_content, "html.parser")
    clean_text = soup.get_text(separator="\n", strip=True)
    return clean_text