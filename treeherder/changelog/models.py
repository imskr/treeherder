from django.db import models


class Changelog(models.Model):
    id = models.CharField(max_length=255, primary_key=True)
    date = models.DateTimeField(db_index=True)
    author = models.CharField(max_length=100)
    owner = models.CharField(max_length=100)
    project = models.CharField(max_length=100)
    project_url = models.CharField(max_length=360)
    message = models.CharField(max_length=360)
    description = models.CharField(max_length=360)
    type = models.CharField(max_length=100)
    url = models.CharField(max_length=360)

    class Meta:
        db_table = 'changelog_entry'

    def __str__(self):
        return "[%s] %s by %s" % (self.id, self.message, self.author)


class ChangelogFile(models.Model):
    id = models.AutoField(primary_key=True)
    changelog = models.ForeignKey(Changelog, related_name='files', on_delete=models.CASCADE)
    name = models.SlugField(max_length=255, unique=True)

    class Meta:
        db_table = 'changelog_file'

    def __str__(self):
        return self.name
