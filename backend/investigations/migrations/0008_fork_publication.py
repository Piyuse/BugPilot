from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("investigations", "0007_automatic_default_branch")]
    operations = [
        migrations.AddField(model_name="investigation", name="publication_repository_url", field=models.URLField(blank=True, max_length=500)),
        migrations.AddField(model_name="investigation", name="publication_head_owner", field=models.CharField(blank=True, max_length=100)),
        migrations.AddField(model_name="investigation", name="publication_attempt", field=models.PositiveIntegerField(default=0)),
    ]
