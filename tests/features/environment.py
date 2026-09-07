def after_scenario(context, scenario):
    context.repository.cleanup()
