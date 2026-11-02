from django.db import migrations, models


def retire_sample_data(apps, schema_editor):
    # Keep historical records intact, but exclude seeded placeholders from discovery.
    Repository = apps.get_model("investigations", "Repository")
    Repository.objects.using(schema_editor.connection.alias).filter(
        url__istartswith="https://github.com/bugpilot-demo/"
    ).update(is_active=False)


class Migration(migrations.Migration):
    dependencies = [("investigations", "0009_remove_bugreport")]

    operations = [
        migrations.AlterField(
            model_name="investigation",
            name="mode",
            field=models.CharField(choices=[("live", "Live")], default="live", max_length=10),
        ),
        migrations.RunPython(retire_sample_data, migrations.RunPython.noop),
    ]
