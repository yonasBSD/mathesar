# Generated by Django 4.2.11 on 2024-10-15 06:42

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('mathesar', '0018_remove_datafile_table_imported_to_and_more'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='sharedtable',
            name='table',
        ),
        migrations.DeleteModel(
            name='SharedQuery',
        ),
        migrations.DeleteModel(
            name='SharedTable',
        ),
    ]
