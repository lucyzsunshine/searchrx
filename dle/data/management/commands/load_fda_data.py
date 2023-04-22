import datetime
import json
import logging
import os
import re
import shutil
import urllib.request as request
from contextlib import closing
from distutils.util import strtobool
from zipfile import ZipFile

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.utils import IntegrityError, OperationalError

import requests

# from data.constants import FDA_SECTION_NAME_MAP
from data.models import DrugLabel, LabelProduct, ParsingError, ProductSection
from data.util import check_recently_updated, strfdelta  # PDFParseException, convert_date_string


# from bs4 import BeautifulSoup

# from users.models import MyLabel


logger = logging.getLogger(__name__)


# python manage.py load_fda_data --type test --cleanup False --insert False --count_titles True
# python manage.py load_fda_data --type my_label --my_label_id 9 --cleanup False --insert False
# runs with `python manage.py load_fda_data --type {type}`
class Command(BaseCommand):
    help = "Loads data from FDA"
    re_combine_whitespace = re.compile(r"\s+")
    re_remove_nonalpha_characters = re.compile("[^a-zA-Z ]")
    dl_json_url = "https://api.fda.gov/download.json"
    dl_json = json.loads(requests.get(dl_json_url).text)
    drugs_json = dl_json["results"]["drug"]
    labels_json = dl_json["results"]["drug"]["label"]
    urls = [x["file"] for x in labels_json["partitions"]]

    def __init__(self, stdout=None, stderr=None, no_color=False, force_color=False):
        self.root_dir = settings.MEDIA_ROOT / "fda"
        os.makedirs(self.root_dir, exist_ok=True)
        super().__init__(stdout, stderr, no_color, force_color)

    def add_arguments(self, parser):
        parser.add_argument(
            "--type", type=str, help="full, monthly, test or my_label", default="monthly"
        )
        parser.add_argument("--insert", type=strtobool, help="Set to connect to DB", default=True)
        parser.add_argument("--cleanup", type=strtobool, help="Set to cleanup files", default=False)
        parser.add_argument(
            "--my_label_id", type=int, help="set my_label_id for --type my_label", default=None
        )
        parser.add_argument(
            "--count_titles",
            type=strtobool,
            help="output counts of the section_names",
            default=False,
        )
        parser.add_argument(
            "--skip_more_recent_than_n_hours",
            type=int,
            help="Skip re-scraping labels more recently imported than this number of hours old. Default is 168 (7 days)",
            default=168,  # 7 days; set to 0 to re-scrape everything
        )
        parser.add_argument(
            "--skip_known_errors",
            type=strtobool,
            help="Skip labels that have previously had parsing errors. Default is True",
            default=True,
        )

    """
    Entry point into class from command line
    """

    def handle(self, *args, **options):
        insert = options["insert"]
        self.skip_labels_updated_within_span = datetime.timedelta(
            hours=int(options["skip_more_recent_than_n_hours"])
        )
        self.skip_errors = options["skip_known_errors"]

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

        dl_json_url = "https://api.fda.gov/download.json"
        dl_json = json.loads(requests.get(dl_json_url).text)
        labels_json = dl_json["results"]["drug"]["label"]
        urls = [x["file"] for x in labels_json["partitions"]]
        json_zips = self.download_json(urls)
        self.extract_json_zips(json_zips)
        file_dir = self.root_dir / "record_zips"

        # Iterate the json files in the directory one by one
        for file in os.listdir(file_dir):
            json_file = file_dir / file
            raw_json_result = None
            with open(json_file, encoding="utf-8-sig") as f:
                raw_json_result = json.load(f)
                logger.info(f"start loading json {json_file}")
            raw_json_result = raw_json_result["results"]
            logger.info(f"Finished loading {json_file}")
            filtered_records = self.filter_data(raw_json_result)
            self.import_records(filtered_records, insert)

        cleanup = options["cleanup"]
        logger.debug(f"options: {options}")

        if cleanup:
            self.cleanup(record_zips)
            self.cleanup(filtered_records)

        logger.info("DONE")

    def download_json(self, urls):
        logger.info("Downloading bulk archives.")
        file_dir = self.root_dir / "json_zip"
        os.makedirs(file_dir, exist_ok=True)
        records = []
        for url in urls:
            records.append(self.download_single_json(url, file_dir))
        return records

    def download_single_json(self, url, dest):
        url_filename = url.split("/")[-1]
        file_path = dest / url_filename
        if os.path.exists(file_path):
            logger.info(f"File already exists: {file_path}. Skipping.")
            return file_path
        # Download the drug labels archive file
        with closing(request.urlopen(url)) as r:
            with open(file_path, "wb") as f:
                logger.info(f"Downloading {url} to {file_path}")
                shutil.copyfileobj(r, f)
        return file_path

    def extract_json_zips(self, zips):
        logger.info("Extracting json zips")
        file_dir = self.root_dir / "record_zips"
        os.makedirs(file_dir, exist_ok=True)
        for zip_file in zips:
            with ZipFile(zip_file, "r") as zf:
                for zobj in zf.infolist():
                    if os.path.exists(file_dir / zobj.filename):
                        logger.info(f"Already extracted file {zobj.filename}")
                    else:
                        zf.extract(zobj, file_dir)
                        logger.info(f"Extracted file {zobj.filename}")
        # TODO error handling?
        # return file_dir

    def filter_data(self, raw_json_result):
        results_by_type = {}
        for res in raw_json_result:
            product_type = self.check_type(res)
            if product_type not in results_by_type.keys():
                results_by_type[product_type] = [res]
            else:
                results_by_type[product_type].append(res)
        # We only care about the prescription drugs
        drugs_ref = results_by_type["human_prescription_drug"]
        drugs = []
        for dg in drugs_ref:
            if "is_original_packager" in dg["openfda"].keys():
                drugs.append(dg)

        errors = []
        records = {}
        # restructure to match ema format
        for drug in drugs:
            try:
                info = {}
                info["metadata"] = drug["openfda"]
                info["metadata"]["effective_time"] = drug["effective_time"]

                label_text = {}
                for key, val in drug.items():
                    # for my purposes I didn't need tables (mostly html formatting)
                    if (type(val) == list) and ("table" not in key):
                        label_text[key] = list(set(val))  # de-duplicate contents
                info["Label Text"] = label_text
                records[drug["id"]] = info
            except:
                # TODO raise and capture an Exception, save ParsingError to DB
                errors += [drug]
        return records

    def check_type(self, res):
        if "product_type" not in res["openfda"].keys():
            return "uncategorized_drug"
        pt = res["openfda"]["product_type"]
        if type(pt) == float:
            return "uncategorized_drug"
        if type(pt) == list:
            assert len(pt) == 1
            return pt[0].lower().replace(" ", "_")
        else:
            logger.info(f"Problem determining type: {pt}")

    def import_records(self, filtered_records, insert):
        logger.info("Building Drug Label DB records from JSON")
        for key in filtered_records:
            # If this is a known error, skip it
            # Get a UUID from the JSON file
            record = filtered_records[key]
            try:
                source_product_number = (
                    record["metadata"]["product_ndc"]
                    if "product_ndc" in record["metadata"]
                    else None
                )
                if self.skip_errors:
                    existing_errors = ParsingError.objects.filter(
                        source="FDA", source_product_number=source_product_number
                    )
                    if existing_errors.count() >= 1:
                        logger.warning(
                            self.style.WARNING(
                                f"Label skipped. Known error(s): {existing_errors[0]}."
                            )
                        )
                        continue
            except Exception as e:
                # No source_product_number
                logger.error(f"No source_product_number for {record}")
                logger.error(str(e))
                continue

            # If it has been recently imported, skip it
            existing_labels = DrugLabel.objects.filter(
                source="FDA", source_product_number=source_product_number
            ).order_by("-updated_at")
            if existing_labels.count() >= 1:
                existing_label = existing_labels[0]
                if check_recently_updated(
                    dl=existing_label, skip_timeframe=self.skip_labels_updated_within_span
                ):
                    last_updated_ago = (
                        datetime.datetime.now(datetime.timezone.utc) - existing_label.updated_at
                    )
                    logger.warning(
                        self.style.WARNING(
                            f"Label skipped. Updated {strfdelta(last_updated_ago)} ago, less than {self.skip_labels_updated_within_span}"
                        )
                    )
                    continue

            # Otherwise, continue
            try:
                dl = DrugLabel()
                self.process_json_record(record, dl, insert)
            # TODO handle more specific exceptions
            except Exception as e:
                logger.error(f"Could not parse record {key}")
                logger.error(str(e))
                application_num = (
                    record["metadata"]["application_number"][0][4:]
                    if "application_number" in record["metadata"]
                    else None
                )
                if application_num:
                    url = record["metadata"]["url"] if "url" in record["metadata"] else None
                else:
                    url = ""
                parsing_error, created = ParsingError.objects.get_or_create(
                    source="FDA",
                    source_product_number=source_product_number,
                    message=str(e),
                    url=url,
                    error_type="data_error"
                )
                if created:
                    logger.warning(f"Created new parsing error for {parsing_error}")
                else:
                    logger.warning(f"ParsingError {parsing_error} already exists")
                continue

    def process_json_record(self, record, dl, insert):
        dl.source = "FDA"
        dl.product_name = record["metadata"]["brand_name"]
        dl.generic_name = record["metadata"]["generic_name"]
        dl.version_date = datetime.datetime.strptime(
            record["metadata"]["effective_time"], "%Y%m%d"
        )
        dl.marketer = record["metadata"]["manufacturer_name"]
        # TODO: What does it mean when there are more than one product numbers?
        dl.source_product_number = record["metadata"]["product_ndc"][0]
        
        dl.link = None
        application_num = record["metadata"]["application_number"][0][4:] if "application_number" in record["metadata"] else None
        if application_num is None:
            dl.link = record["metadata"]["url"] if "url" in record["metadata"] else None
        else:
            dl.link = f"https://www.accessdata.fda.gov/scripts/cder/daf/index.cfm?event=overview.process&varApplNo={application_num}"

        dl.raw_rext = record["Label Text"]
        lp = LabelProduct(drug_label=dl)
        try:
            if insert:
                dl.save()
                logger.info(f"Saving new drug label: {dl}")
        except IntegrityError as e:
            logger.error(str(e))
            return
        try:
            if insert:
                lp.save()
                logger.info("Saving new label product")
        except IntegrityError as e:
            logger.error(str(e))
            return

        # Now parse the section text
        for key, value in record["Label Text"].items():
            logger.info(f"Section found: {key}")
            ps = ProductSection(
                label_product=lp,
                section_name=key,
                section_text=value,
            )
            try:
                if insert:
                    ps.save()
                    logger.debug(f"Saving new product section {ps}")
            except IntegrityError as e:
                logger.error(str(e))
            except OperationalError as e:
                logger.error(str(e))

    def cleanup(self, files):
        for file in files:
            logger.debug(f"remove: {file}")
            os.remove(file)
