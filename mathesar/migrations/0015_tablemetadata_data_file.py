# Generated by Django 4.2.11 on 2024-09-10 12:25

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('mathesar', '0014_remove_columnmetadata_duration_show_units_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='tablemetadata',
            name='data_file',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, to='mathesar.datafile'),
        ),
    ]
