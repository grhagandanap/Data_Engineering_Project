from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.providers.postgres.operators.postgres import PostgresOperator
from datetime import datetime, timedelta
import pandas as pd
import numpy as np
import os
import requests


def extract_api_data():
    api_url = "https://my.api.mockaroo.com/order.json?key=b3b26200"
    response = requests.get(api_url)

    try:
        data = response.json()
    except ValueError:
        raise ValueError(f"Invalid JSON response: {response.text}")

     # Proceed if data is valid
    if response.status_code == 200:
        orders_data = pd.DataFrame(data)

        file_path = '/shared/orders.csv'

        # Check if the file exists
        if os.path.exists(file_path):
            # If file exists, append new data
            existing_data = pd.read_csv(file_path)

            combined_data = pd.concat(
                [existing_data, orders_data], ignore_index=True)
            combined_data.to_csv(file_path, index=False)

        else:
            # If file does not exist, create it
            orders_data.to_csv(file_path, index=False)

        print("It's successful!")
    else:
        raise ValueError(
            f"API request failed with status code {response.status_code}")


def load_inventory_data():

    file_path = '/shared/inventory_data.csv'

    if os.path.exists(file_path):
        print("Inventory data processing complete!")
    else:
        inventory_data = pd.read_csv('/opt/airflow/inventory.csv')
        inventory_data.to_csv('/shared/inventory.csv', index=False)


def update_inventory_data():
    # Load the order data (latest 10 records)
    file_path_orders = '/shared/orders.csv'
    file_path_inventory = '/shared/inventory.csv'

    if os.path.exists(file_path_orders) and os.path.exists(file_path_inventory):
        orders_data = pd.read_csv(file_path_orders)
        inventory_data = pd.read_csv(file_path_inventory)

        # Take the first 10 records
        latest_orders = orders_data.head(10)

        # Group by Product and sum the Quantity Ordered
        orders_summary = latest_orders.groupby('product_id', as_index=False)[
            'quantity'].sum().reset_index(drop=True)

        # Merge with inventory data
        updated_inventory = pd.merge(
            inventory_data, orders_summary, how='left', left_on='id', right_on='product_id')

        # Update the stock and ensure missing values are handled as 0 and subtract the quantity
        updated_inventory['stock_quantity'] = updated_inventory['stock_quantity'] - \
            updated_inventory['quantity'].fillna(0)

        # Convert the result to an integer
        updated_inventory['stock_quantity'] = np.where(updated_inventory['stock_quantity'] < 0, 0,
                                                       updated_inventory['stock_quantity']).astype(int)

        # Drop unnecessary columns
        updated_inventory = updated_inventory[[
            'id', 'name', 'stock_quantity', 'price']]

        # Save updated inventory back to CSV
        updated_inventory.to_csv(file_path_inventory, index=False)

        print("Inventory updated successfully!")
    else:
        raise FileNotFoundError("Order data or inventory data file not found")


# Define the DAG
default_args = {
    'owner': 'airflow',
    'start_date': datetime(2024, 9, 18),
    'retries': 5,
    'retry_delay': timedelta(minutes=5)
}

with DAG(dag_id='etl_dag_v45',
         default_args=default_args,
         schedule_interval='@daily',
         catchup=True) as dag:

    # Define the tasks
    extract_task = PythonOperator(
        task_id='extract_api_data',
        python_callable=extract_api_data
    )

    load_inventory_task = PythonOperator(
        task_id='load_inventory_data',
        python_callable=load_inventory_data
    )

    update_inventory_task = PythonOperator(
        task_id='update_inventory_data',
        python_callable=update_inventory_data
    )

    create_inventory_table = PostgresOperator(
        task_id='create_inventory_table',
        postgres_conn_id='postgres_localhost',
        sql="""
        CREATE TABLE IF NOT EXISTS inventory (
            id SERIAL PRIMARY KEY,
            name VARCHAR(255),
            stock_quantity INT,
            price DECIMAL(10,2)
        );
    """
    )

    create_orders_table = PostgresOperator(
        task_id='create_orders_table',
        postgres_conn_id='postgres_localhost',
        sql="""
            CREATE TABLE IF NOT EXISTS orders (
                id VARCHAR PRIMARY KEY,
                customer_id INT,
                product_id INT,
                order_date DATE,
                quantity INT
            );
        """
    )

    # Insert data into the inventory table before inserting orders
    insert_inventory_data = PostgresOperator(
        task_id='insert_inventory_data',
        postgres_conn_id='postgres_localhost',
        sql="""
            TRUNCATE TABLE inventory;
            COPY inventory(id, name, stock_quantity, price)
            FROM '/shared/inventory.csv'
            DELIMITER ',' CSV HEADER;
        """
    )

    # Insert data into the orders table after ensuring inventory is populated
    insert_orders_data = PostgresOperator(
        task_id='insert_orders_data',
        postgres_conn_id='postgres_localhost',
        sql="""
            TRUNCATE TABLE orders;
            COPY orders(id, customer_id, product_id, order_date, quantity)
            FROM '/shared/orders.csv'
            DELIMITER ',' CSV HEADER;
        """
    )

    # Set the task dependencies
    extract_task >> load_inventory_task >> update_inventory_task >> [
        create_orders_table, create_inventory_table]
    create_inventory_table >> insert_inventory_data 
    create_orders_table >> insert_orders_data
