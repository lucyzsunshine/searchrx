from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError
from requests.exceptions import ChunkedEncodingError
from data.models import (
    DrugLabel,
    LabelProduct,
    ProductSection,
)
from users.models import MyLabel
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.conf import settings
import requests
import fitz  # PyMuPDF
from bs4 import BeautifulSoup
import re
import datetime
import pandas as pd
import time
import logging
import random
import string
from selenium import webdriver
from selenium.webdriver.chrome.options import Options

logger = logging.getLogger(__name__)

TGA_BASE_URL = "https://www.ebs.tga.gov.au/ebs/picmi/picmirepository.nsf/"

# runs with `python manage.py load_tga_data`
# add `--type full` to import the full dataset
# add `--verbosity 2` for info output
# add `--verbosity 3` for debug output
class Command(BaseCommand):
    count = 0 # TODO: Remove once pdf parser is supported

    help = "Loads data from TGA"

    def __init__(self, stdout=None, stderr=None, no_color=False, force_color=False):
        super().__init__(stdout, stderr, no_color, force_color)
        self.num_drug_labels_parsed = 0
        "keep track of the number of labels processed"
        self.error_urls = {}
        "dictionary to keep track of the urls that have parsing errors; form: {url: True}"

    def add_arguments(self, parser):
        parser.add_argument(
            "--type",
            type=str,
            help="'full'",
            default="full",
        )

    def handle(self, *args, **options):
        import_type = options["type"]
        if import_type not in ["full"]:
            raise CommandError(
                "'type' parameter must be 'full'"
            )

        # basic logging config is in settings.py
        # verbosity is 1 by default, gives critical, error and warning output
        # `--verbosity 2` gives info output
        # `--verbosity 3` gives debug output
        verbosity = int(options["verbosity"])
        root_logger = logging.getLogger("")
        if verbosity == 2:
            root_logger.setLevel(logging.INFO)
        elif verbosity == 3:
            root_logger.setLevel(logging.DEBUG)

        logger.info(self.style.SUCCESS("start process"))
        logger.info(f"import_type: {import_type}")
        
        urls = self.get_tga_pi_urls()

        logger.info(f"total urls to process: {len(urls)}")

        # Before being able to access the PDFs, we have to accept the access terms
        # Then store the cookies and pass it to the requests
        chrome_options = Options()
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--headless')
        driver = webdriver.Chrome(options=chrome_options)
        driver.get(TGA_BASE_URL + "/pdf?OpenAgent")
        time.sleep(1)
        button = driver.find_element_by_xpath('/html/body/form/div[2]/div[3]/div[1]/a[1]')
        driver.execute_script("arguments[0].click();", button)
        time.sleep(5)
        driver_cookies = driver.get_cookies()
        cookies = {c['name']:c['value'] for c in driver_cookies}

        # Iterate all the query URLs
        for url in urls:
            logger.info(f"processing url: {url}")
            # Grab the webpage
            response = requests.get(url)
            soup = BeautifulSoup(response.text, "html.parser")
            table = soup.find("table")
            table_body = table.find("tbody")
            rows = table_body.find_all("tr")
            # Iterate all the products in the table
            for row in rows:
                try:
                    dl = self.get_drug_label_from_row(soup, row)
                    logger.debug(repr(dl))
                    # dl.link is url of pdf
                    # for now, assume only one LabelProduct per DrugLabel
                    lp = LabelProduct(drug_label=dl)
                    lp.save()
                    dl.raw_text = self.parse_pdf(dl.link, lp, cookies)
                    dl.save()
                    self.num_drug_labels_parsed += 1
                except IntegrityError as e:
                    logger.warning(self.style.WARNING("Label already in db"))
                    logger.debug(e, exc_info=True)
                except AttributeError as e:
                    logger.warning(self.style.ERROR(repr(e)))
                except ValueError as e:
                    logger.warning(self.style.WARNING(repr(e)))
                logger.info(f"sleep 1s")
                time.sleep(1)

            for url in self.error_urls.keys():
                logger.warning(self.style.WARNING(f"error parsing url: {url}"))

        logger.info(f"num_drug_labels_parsed: {self.num_drug_labels_parsed}")
        logger.info(self.style.SUCCESS("process complete"))
        return

    def get_drug_label_from_row(self, soup, row):
        dl = DrugLabel()  # empty object to populate as we go
        dl.source = "TGA5"

        columns = row.find_all("td")
        # product name is located at the fitst column
        dl.product_name = columns[0].text.strip()
        # pdf link is located at the second column
        dl.link = TGA_BASE_URL + columns[1].find("a")["href"]
        dl.source_product_number = columns[1].find("a")["target"] #TODO: what is this product number?
        # active ingredient(s) at the third column
        dl.generic_name = columns[2].text.strip()
        # get version date from the footer
        footer = soup.find("div", {"class": "Footer"})
        date_str_key = "Last updated:"
        entry = footer.find_next(string=re.compile(date_str_key))
        entry = entry.strip()
        sub_str = entry[len(date_str_key) :].strip()
        # parse sub_str into date, from DD M YYYY to: YYYY-MM-DD
        dt_obj = datetime.datetime.strptime(sub_str, "%d %B %Y")
        str = dt_obj.strftime("%Y-%m-%d")
        dl.version_date = str

        dl.save()
        return dl

    def get_backoff_time(self, tries=5):
        """Get an amount of time to backoff. Starts with no backoff.
        Returns: number of seconds to wait
        """
        # starts with no backoff
        yield 0
        # then we have an exponential backoff with jitter
        for i in range(tries - 1):
            yield 2**i + random.uniform(0, 1)

    def parse_pdf(self, pdf_url, lp, cookies):
        # have a backoff time for pulling the pdf from the website
        for t in self.get_backoff_time(5):
            try:
                logger.info(f"time to sleep: {t}")
                time.sleep(t)
                response = requests.get(pdf_url, cookies=cookies)
                break  # no Exception means we were successful
            except (ValueError, ChunkedEncodingError) as e:
                logger.error(self.style.ERROR(f"caught error: {e.__class__.__name__}"))
                logger.warning(self.style.WARNING("Unable to read url, may continue"))
                response = None
        if not response:
            logger.error(self.style.ERROR("unable to grab url contents"))
            self.error_urls[pdf_url] = True
            return "unable to download pdf"

        filename = default_storage.save(
            settings.MEDIA_ROOT / f"tga_{self.count}.pdf", ContentFile(response.content)
        )
        logger.info(f"saved file to: {filename}")

        tga_file = settings.MEDIA_ROOT / f"tga_{self.count}.pdf"
        self.count += 1
        raw_text = self.process_tga_file(tga_file, lp, pdf_url)
        # delete the file when done
        #default_storage.delete(filename)
        return raw_text

    def process_tga_file(self, tga_file, lp, pdf_url=""):
        # TODO: Parse the pdf file here
        raw_text = ""
        logger.info("Stubbed out - Success")
        return raw_text

    def get_tga_pi_urls(self):
        """
        The TGA query address is https://www.ebs.tga.gov.au/ebs/picmi/picmirepository.nsf/PICMI?OpenForm&t=PI&k=0&r=/
        Iterate the query parameter k with 0-9 and A-Z
        """
        URLs = []
        for i in range(0, 10): # queries 0-9
            base_url = f"https://www.ebs.tga.gov.au/ebs/picmi/picmirepository.nsf/PICMI?OpenForm&t=PI&k={i}&r=/"
            URLs.append(base_url)
        for c in string.ascii_uppercase:
            base_url = f"https://www.ebs.tga.gov.au/ebs/picmi/picmirepository.nsf/PICMI?OpenForm&t=PI&k={c}&r=/"
            URLs.append(base_url)
        return URLs