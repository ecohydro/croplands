from flask import current_app
from gfsad import celery
from gfsad.models import db, TimeSeries, Location, Record
from gfsad.utils import split_list
from gfsad.utils.fusion import replace_rows
from gfsad.utils.mappings import get_crop_label, get_intensity_label, get_land_cover_label, \
    get_water_label
import boto
from boto.s3.key import Key
import datetime
import StringIO
import gzip
import json
from gfsad.views.gee import init_gee, extract_info
import csv


def status(so_far, total):
    print '%d bytes transferred out of %d' % (so_far, total)


def convert_to_labels(row):
    row['water_source'] = get_water_label(row['water_source'])
    row['crop_primary'] = get_crop_label(row['crop_primary'])
    row['crop_secondary'] = get_crop_label(row['crop_secondary'])
    row['land_cover'] = get_land_cover_label(row['land_cover'])
    row['intensity'] = get_intensity_label(row['intensity'])
    return row


def median(A):
    if len(A) == 0:
        return None
    return sorted(A)[len(A) / 2]

def mean(A):
    total = 0.0
    count = 0

    for i in A:
        if i is not None:
            total += i
            count += 1

    return total / count

@celery.task(rate_limit="120/m", time_limit=300)
def get_ndvi(id):
    record = db.session.query(Record).filter(Record.id == id).first()

    init_gee()
    data = extract_info(lat=record.location.lat, lon=record.location.lon,
                        date_start=str(record.year) + "-01-01", date_end=str(record.year) + "-12-31")

    series = [[] for i in range(0, 12)]

    for row in data:
        if row['ndvi'] is None:
            continue
        month = int(row['date'].split('-')[1])
        series[month - 1].append(int(row['ndvi'] / 10))

    series12 = [median(month) for month in series]

    record.ndvi_mean = int(mean(series12))
    record.ndvi = series12

    print("Record #%d NDVI Updated. Mean: %d" % (record.id, record.ndvi_mean))
    db.session.commit()


@celery.task()
def sum_ratings_for_record(id):
    try:
        db.session.execute(
            """
            UPDATE record
            SET rating = (SELECT sum(rating)
                          FROM record_rating
                          WHERE record_rating.record_id = :id)
            WHERE id = :id
            """,
            {'id': id}
        )
        db.session.commit()
    except Exception as e:
        print e
        pass


@celery.task(rate_limit="15/h", time_limit=300)
def build_fusion_tables():
    cmd = """
          SELECT  record.id as id,
                  location.id AS location_id,
                  location.lat AS lat,
                  location.lon AS lon,
                  record.rating as rating,
                  record.year as year,
                  record.month as month,
                  record.land_use_type as land_cover,
                  record.crop_primary as crop_primary,
                  record.crop_secondary as crop_secondary,
                  record.water as water_source,
                  record.intensity as intensity,
                  location.country as country,
                  record.source_description as source_description,
                  record.source_type as source_type,
                  record.source_id as source_id,
                  record.source_class as source_class,
                  images.image_1 as image_1,
                  images.image_2 as image_2,
                  images.image_3 as image_3,
                  location.use_validation as use_validation,
                  location.use_private as use_private
          FROM record
          LEFT JOIN location ON location.id = record.location_id
          LEFT OUTER JOIN (SELECT * from crosstab (
            $$SELECT location_id, url, replace(url, 'images/', 'http://images.croplands.org/')
             FROM   image
             WHERE  url not like '%%digital_globe%%'
             $$)
             AS t ( location_id int,
              image_1 text, --varchar changed to text with replace function above
              image_2 text,
              image_3 text
            )
          ) images on location.id = images.location_id
          WHERE location.use_deleted is false AND location.use_invalid is false
          """

    result = db.engine.execute(cmd)
    columns = result.keys()

    all_results = [convert_to_labels(dict(row.items())) for row in result]

    training = StringIO.StringIO()
    validation = StringIO.StringIO()
    public = StringIO.StringIO()

    writer_training = csv.DictWriter(training, fieldnames=columns[0:-2], extrasaction='ignore')
    writer_validation = csv.DictWriter(validation, fieldnames=columns[0:-2], extrasaction='ignore')
    writer_public = csv.DictWriter(public, fieldnames=columns[0:-2], extrasaction='ignore')

    writer_training.writeheader()
    writer_validation.writeheader()
    writer_public.writeheader()

    print len(all_results)

    for row in all_results:
        try:
            if not row['use_private']:
                writer_public.writerow(row)
            if row['use_validation']:
                writer_validation.writerow(row)
            else:
                row['intensity'] = 'Unknown'
                writer_training.writerow(row)
        except UnicodeEncodeError as e:
            print e, row

    replace_rows('1C_gFvQmd3AGtB0Q0XgnKk5ESUARSH79FB9Un8sF2', training, startLine=1)
    replace_rows('12WLGpk7o1ic_j88NQfmrUEILVWDlrJaqZCAqEDeo', validation, startLine=1)
    replace_rows('1jQjTg7zXhwmLGJdfPCavgdifnyNTqJGi3Bn3RwWF', public, startLine=1)


@celery.task(rate_limit="15/h", time_limit=300)
def build_static_records():
    NUM_FILES = 3

    LICENSE = """This data is made available under the Open Database License:
    http://opendatacommons.org/licenses/odbl/1.0/. Any rights in individual
    contents of the database are licensed under the Database Contents License:
    http://opendatacommons.org/licenses/dbcl/1.0/"""

    ATTRIBUTION = 'Global Food Security Analysis-Support Data at 30m, https://croplands.org'

    cmd = """
          SELECT  location.id AS location_id,
                  to_char(record.date_updated, 'yyyy-mm-dd') AS date_updated,
                  location.lat AS lat,
                  location.lon AS lon,
                  CAST(use_validation as INTEGER) AS use_validation,
                  record.id as id,
                  record.rating as rating,
                  record.year as year,
                  record.month as month,
                  record.land_use_type as land_use_type,
                  record.crop_primary as crop_primary,
                  record.crop_secondary as crop_secondary,
                  record.water as water,
                  record.intensity as intensity
          FROM record
          LEFT JOIN location
          ON location.id = record.location_id
          WHERE location.use_private is false AND location.use_deleted is false AND location.use_invalid is false
          ORDER BY random()
          """

    result = db.engine.execute(cmd)
    columns = result.keys()
    records = [
        [row['location_id'], row['date_updated'], row['lat'], row['lon'], row['use_validation'],
         row['id'], row['rating'],
         row['year'], row['month'], row['land_use_type'], row['crop_primary'],
         row['crop_secondary'],
         row['water'],
         row['intensity']] for row in result]

    split_records = split_list(records, NUM_FILES)

    # Connect to S3
    s3 = boto.connect_s3(current_app.config['AWS_ACCESS_KEY_ID'],
                         current_app.config['AWS_SECRET_ACCESS_KEY'])

    # Get bucket
    bucket = s3.get_bucket(current_app.config['AWS_S3_BUCKET'])

    for i in range(1, NUM_FILES + 1):
        if current_app.testing:
            key = 'public/json/records.test.p%d.json'
        else:
            key = 'public/json/records.p%d.json'

        content = {
            'num_results': len(records),
            'total_pages': NUM_FILES,
            'page': i,
            'meta': {
                'created': datetime.datetime.utcnow().isoformat(),
                'columns': columns,
                'license': LICENSE,
                'attribution': ATTRIBUTION
            },
            'objects': split_records[i - 1]
        }

        if i < NUM_FILES - 1:
            content['next_page'] = key % (i + 1)

        # fake a file for gzip
        out = StringIO.StringIO()

        k = Key(bucket)

        k.key = key % i

        k.set_metadata('content-type', 'application/javascript')
        k.set_metadata('cache-control', 'max-age=300')
        k.set_metadata('content-encoding', 'gzip')

        with gzip.GzipFile(fileobj=out, mode="w") as outfile:
            outfile.write(json.dumps(content))

        if current_app.testing:
            k.set_contents_from_string(out.getvalue(), cb=status, num_cb=10)
        else:
            k.set_contents_from_string(out.getvalue())
        k.make_public()