from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("investigations", "0005_alter_investigation_mode")]
    operations = [
        migrations.RemoveConstraint(model_name="investigation", name="one_investigation_per_ticket_mode"),
        # Existing records must retain their Jira identity and comment checkpoints.
        migrations.AddField(model_name="investigation", name="issue_source",
                            field=models.CharField(max_length=10, choices=[("github", "GitHub"), ("jira", "Jira")], default="jira")),
        migrations.AddField(model_name="investigation", name="source_repository_url",
                            field=models.URLField(max_length=500, blank=True)),
        migrations.AddField(model_name="investigation", name="source_issue_number",
                            field=models.PositiveBigIntegerField(null=True, blank=True)),
        migrations.AlterField(model_name="investigation", name="issue_key", field=models.CharField(max_length=400)),
        migrations.RenameField(model_name="investigation", old_name="jira_linked", new_name="issue_linked"),
        migrations.RenameField(model_name="investigation", old_name="jira_comment_id", new_name="issue_comment_id"),
        migrations.RenameField(model_name="investigation", old_name="jira_comment_url", new_name="issue_comment_url"),
        migrations.AlterField(model_name="investigation", name="issue_source",
                              field=models.CharField(max_length=10, choices=[("github", "GitHub"), ("jira", "Jira")], default="github")),
        migrations.AddConstraint(model_name="investigation",
            constraint=models.UniqueConstraint(fields=["issue_source", "issue_key", "mode"], name="one_investigation_per_source_ticket_mode")),
    ]
