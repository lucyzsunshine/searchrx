# Generated by Django 4.2 on 2023-04-18 14:29

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('data', '0010_rename_errortype_parsingerror_error_type'),
    ]

    operations = [
        migrations.AlterField(
            model_name='parsingerror',
            name='error_type',
            field=models.CharField(blank=True, choices=[('version_date_empty', 'Version date empty'), ('version_date_parse', 'Version date parsed failure'), ('pdf_error', 'Failed to parse PDF'), ('link_error', 'Could not generate PDF link'), ('data_error', 'DataError'), ('no_pdf', 'No PDF')], default=None, max_length=30),
        ),
    ]
