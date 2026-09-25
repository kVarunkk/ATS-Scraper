import xml.etree.ElementTree as ET
import requests
from tenacity import retry, stop_after_attempt, wait_exponential
from utils.extract_text import extract_text

PERSONIO_JOBS_URL = "https://{slug}.jobs.personio.de/xml"

XML_JOB_TYPE_MAP = {
    "full-time": "Fulltime",
    "part-time": "Intern",
    "trainee": "Intern",
    "permanent": "Fulltime",
    "contract": "Contract",
    "temporary": "Contract",
    "intern": "Intern",
}

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), reraise=True)
def _fetch_xml_feed(slug: str):
    resp = requests.get(PERSONIO_JOBS_URL.format(slug=slug), timeout=20)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.text


def _format_salary_xml(salary_elem) -> tuple[str | None, float | None, float | None]:
    if not salary_elem:
        return None, None, None
    try:
        min_elem = salary_elem.find("min")
        max_elem = salary_elem.find("max")
        curr_elem = salary_elem.find("currencySymbol") or salary_elem.find("currencyCode")
        
        lo = float(min_elem.text) if min_elem is not None and min_elem.text else None
        hi = float(max_elem.text) if max_elem is not None and max_elem.text else None
        currency = curr_elem.text if curr_elem is not None and curr_elem.text else ""
        
        if lo is not None and hi is not None:
            return f"{currency} {lo:,.0f} - {hi:,.0f}", lo, hi
        elif lo is not None:
            return f"{currency} {lo:,.0f}", lo, None
    except (ValueError, TypeError):
        pass
    return None, None, None


def fetch_jobs(slug: str, company_url: str, company_name: str = "") -> list[dict]:
    """Returns normalized job dicts ready for db.upsert_job from an XML feed. 
    Empty list if the feed doesn't exist or has no jobs.
    """
    xml_data = _fetch_xml_feed(slug)
    if not xml_data:
        return []

    try:
        root = ET.fromstring(xml_data)
    except ET.ParseError as e:
        print(e)
        return []

    jobs = []
    for position in root.findall(".//position"):
        try:    
            job_id = position.findtext("id")
            job_name = position.findtext("name")
            subcompany = position.findtext("subcompany") or company_name
            
            # Construct job URL using company base URL and position ID if explicit URL is absent
            job_url = f"{company_url.rstrip('/')}/{job_id}" if job_id else company_url
    
            # Aggregate and extract all job description blocks
            description_parts = []
            for jd in position.findall(".//jobDescription"):
                val = jd.findtext("value")
                if val:
                    description_parts.append(val)
            
            description_text = extract_text("".join(description_parts))
    
            # Extract location info from office tag
            office = position.findtext("office")
            location_names = [office.strip()] if office else []
    
            # Determine employment type based on schedule or employmentType
            schedule = (position.findtext("schedule") or "").strip().lower()
            emp_type = (position.findtext("employmentType") or "").strip().lower()
            job_type_key = schedule if schedule else emp_type
            job_type = XML_JOB_TYPE_MAP.get(job_type_key, "Fulltime")
    
            # Format salary information
            salary_elem = position.find("salaryInformation")
            salary_range, salary_min, salary_max = _format_salary_xml(salary_elem)
    
            jobs.append({
                "job_url": job_url,
                "locations": location_names,
                "job_type": job_type,
                "salary_range": salary_range,
                "salary_min": salary_min,
                "salary_max": salary_max,
                "job_name": job_name,
                "description": description_text,
                "company_url": company_url,
                "company_name": subcompany,
                "platform": "workzag",
            })
        except Exception as err:
            failed_id = position.findtext("id") if position is not None else "unknown"
            print(f"Failed parsing position ID {failed_id} in feed: {err}")
            continue    

        
    return jobs