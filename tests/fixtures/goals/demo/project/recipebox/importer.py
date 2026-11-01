"""Importing saved posts into the cookbook."""
from recipebox import classify, parse


def import_posts(posts, book):
    """Add each post to `book`: a post that is not a recipe to "not a recipe", a recipe
    post as a recipe once its caption parses (until then it stays unlisted). Returns it."""
    known = {recipe["id"] for recipe in book["recipes"]}
    for post in posts:
        caption = post.get("caption", "")
        if not classify.is_recipe(caption):
            if post["id"] not in book["not_a_recipe"]:
                book["not_a_recipe"].append(post["id"])
            continue
        recipe = parse.parse_recipe(caption)
        if recipe is not None and post["id"] not in known:
            book["recipes"].append({"id": post["id"], **recipe})
            known.add(post["id"])
    return book
