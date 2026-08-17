"""The demonstration runner: drives the storefront from outside and reads every claim back.

This package never inspects the database to establish an outcome. It seeds fixtures (setup) and then
speaks only HTTP, exactly as a buyer would, so that everything it reports is something the store
itself said.
"""
